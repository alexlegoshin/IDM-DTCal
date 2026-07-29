"""
Графический интерфейс DTCal (классический Tkinter + ttk).

Минималистичный, светлый, без лишних зависимостей — Tkinter поставляется
вместе с Python и легко собирается в portable exe. Интерфейс переиспользует
то же ядро, что и CLI (orchestrate.run_measurement_session, visa_backend,
selftest), поэтому логика измерения полностью идентична.

Устройство:
  - при старте в фоне выполняется предполётная проверка (NI-VISA + самотесты);
    кнопка «Старт» разблокируется только если проверка пройдена — это
    защита оборудования от запуска на сломанном коде/без VISA;
  - измерение идёт в отдельном потоке, весь вывод ядра (print) перехватывается
    в журнал; кнопка «Стоп» кооперативно прерывает проход между точками;
  - графиков нет — результат (данные + погрешность) пишется в .xlsx, путь
    к файлу и сводка по погрешности выводятся в журнал по завершении.
"""
import io
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from apppaths import default_data_dir
from config import ConfigManager
from cli import make_result_filename, validate_measure_params
from sensors import SENSOR_MODELS


ACCENT = "#2563eb"
ACCENT_ACTIVE = "#1d4ed8"
BG = "#f4f5f7"
CARD = "#ffffff"
OK_COLOR = "#15803d"
ERR_COLOR = "#b91c1c"
BUSY_COLOR = "#b45309"
MUTED = "#6b7280"


class _QueueWriter(io.TextIOBase):
    """Файлоподобный объект: перенаправляет stdout/stderr ядра в очередь событий GUI."""

    def __init__(self, events: queue.Queue):
        self.events = events

    def write(self, s):
        if s:
            self.events.put(("log", s))
        return len(s)

    def flush(self):
        pass


class DTCalGUI:
    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.data_dir = Path(getattr(args, "data_dir", None) or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_mgr = ConfigManager(self.data_dir / "dtcal_config.json")

        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._preflight_ok = False

        self.skip_selftest_var = tk.BooleanVar(value=bool(getattr(args, "skip_selftest", False)))
        self.model_var = tk.StringVar(value=next(iter(SENSOR_MODELS)))

        self._closing = False
        self._after_id = None

        self._build_style()
        self._build_ui()
        self._prefill_from_config()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.root.after(120, self._drain_events)
        self._run_preflight()

    def _build_style(self):
        self.root.title("DTCal")
        self.root.geometry("760x680")
        self.root.minsize(680, 560)
        self.root.configure(bg=BG)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = ("Segoe UI", 10)
        style.configure(".", font=base_font, background=BG)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG)
        style.configure("Card.TLabel", background=CARD)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, font=("Segoe UI Semibold", 17))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TLabelframe", background=BG, bordercolor="#d1d5db")
        style.configure("TLabelframe.Label", background=BG, foreground="#374151",
                        font=("Segoe UI Semibold", 10))
        style.configure("TEntry", padding=4)
        style.configure("TButton", padding=(12, 6))
        style.configure("Accent.TButton", padding=(16, 8), foreground="white",
                        background=ACCENT, font=("Segoe UI Semibold", 10), borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", ACCENT_ACTIVE), ("disabled", "#9ca3af")])
        style.configure("Danger.TButton", padding=(14, 8))

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 14, 18, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="DTCal", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Калибровка датчиков ДТ100А1 / ДТ500А1",
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w")

        self.status_dot = tk.Canvas(header, width=12, height=12, bg=BG, highlightthickness=0)
        self.status_dot.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 8))
        self._dot = self.status_dot.create_oval(2, 2, 10, 10, fill=MUTED, outline="")
        self.status_label = ttk.Label(header, text="Инициализация…", style="Muted.TLabel")
        self.status_label.grid(row=0, column=2, rowspan=2, sticky="e")

        body = ttk.Frame(self.root, padding=(18, 6, 18, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, minsize=340)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_params(body)
        self._build_log(body)

        footer = ttk.Frame(self.root, padding=(18, 6, 18, 12))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.footer_label = ttk.Label(footer, text="", style="Muted.TLabel")
        self.footer_label.grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Проверить снова", command=self._run_preflight).grid(row=0, column=1, sticky="e")

    def _build_params(self, parent):
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)

        mdl = ttk.Labelframe(left, text="Модель датчика", padding=10)
        mdl.grid(row=0, column=0, sticky="ew")
        for i, (name, i_nom) in enumerate(SENSOR_MODELS.items()):
            ttk.Radiobutton(mdl, text=f"{name}  (I ном. {i_nom:g} А)", value=name,
                            variable=self.model_var).grid(row=i, column=0, sticky="w")

        pf = ttk.Labelframe(left, text="Параметры измерения (ток возбуждения)", padding=10)
        pf.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        pf.columnconfigure(1, weight=1)

        self.e_start = self._param_row(pf, 0, "Начало", "А")
        self.e_stop = self._param_row(pf, 1, "Конец", "А")
        self.e_step = self._param_row(pf, 2, "Шаг", "А")
        self.e_vlimit = self._param_row(pf, 3, "Огр. напряжения источника", "В")
        self.e_delay = self._param_row(pf, 4, "Задержка установки", "с")
        self.e_cool = self._param_row(pf, 5, "Задержка охлаждения", "с")

        ttk.Label(pf, text="Комментарий").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.e_label = ttk.Entry(pf)
        self.e_label.grid(row=6, column=1, columnspan=2, sticky="ew", pady=(6, 0))

        adv = ttk.Labelframe(left, text="Приборы (необязательно, иначе автопоиск)", padding=10)
        adv.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        adv.columnconfigure(1, weight=1)
        self.e_dmm = self._addr_row(adv, 0, "Мультиметр VISA")
        self.e_src = self._addr_row(adv, 1, "Источник тока VISA")
        self.e_relay = self._addr_row(adv, 2, "Порт реле (COMx)")

        actions = ttk.Frame(left)
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(actions, text="▶  Старт измерения", style="Accent.TButton",
                                    command=self._start_measurement, state="disabled")
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text="■  Стоп", style="Danger.TButton",
                                   command=self._request_stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Checkbutton(left, text="Игнорировать самотесты (не рекомендуется)",
                        variable=self.skip_selftest_var,
                        command=self._run_preflight).grid(row=4, column=0, sticky="w", pady=(8, 0))

    def _param_row(self, parent, row, label, unit=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent, width=12)
        entry.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 6))
        ttk.Label(parent, text=unit, style="Muted.TLabel", width=3).grid(row=row, column=2, sticky="w", pady=3)
        return entry

    def _addr_row(self, parent, row, label):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent)
        entry.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 0))
        return entry

    def _build_log(self, parent):
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.log = scrolledtext.ScrolledText(right, wrap="word", height=10,
                                             font=("Consolas", 9), bg="#0f172a", fg="#e2e8f0",
                                             insertbackground="#e2e8f0", relief="flat", padx=10, pady=8)
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")

    def _prefill_from_config(self):
        saved = self.config_mgr.load()
        if not saved:
            return
        if saved.get("model") in SENSOR_MODELS:
            self.model_var.set(saved["model"])
        mapping = {
            "I_start": self.e_start, "I_stop": self.e_stop, "I_step": self.e_step,
            "V_limit": self.e_vlimit, "delay": self.e_delay, "cooling_delay": self.e_cool,
            "label": self.e_label,
        }
        for key, entry in mapping.items():
            val = saved.get(key)
            if val is not None:
                entry.delete(0, "end")
                entry.insert(0, str(val))

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text, kind="muted"):
        color = {"ok": OK_COLOR, "error": ERR_COLOR, "busy": BUSY_COLOR}.get(kind, MUTED)
        self.status_label.configure(text=text, foreground=color)
        self.status_dot.itemconfigure(self._dot, fill=color)

    # --------------------------------------------------------------- preflight
    def _run_preflight(self):
        self._preflight_ok = False
        self.start_btn.configure(state="disabled")
        self._set_status("Проверка NI-VISA и самотестов…", "busy")
        self.footer_label.configure(text="Идёт предполётная проверка…")
        threading.Thread(target=self._preflight_worker, daemon=True).start()

    def _preflight_worker(self):
        try:
            from visa_backend import check_visa
            visa = check_visa()
            self.events.put(("log", "[VISA] " + visa.summary_line() + "\n"))
            if not visa.ok:
                self.events.put(("preflight", (False, "NI-VISA не найдена", visa.message)))
                return

            if self.skip_selftest_var.get():
                self.events.put(("preflight", (True, visa.summary_line() + " · самотесты пропущены", visa.message)))
                return

            from selftest import run_selftests
            self.events.put(("log", "[Самотесты] запуск виртуальной проверки кода…\n"))
            st = run_selftests()
            self.events.put(("log", f"[Самотесты] {st.summary}\n"))
            status = visa.summary_line() + (" · самотесты OK" if st.ok else " · САМОТЕСТЫ ПРОВАЛЕНЫ")
            self.events.put(("preflight", (st.ok, status, st.output if not st.ok else visa.message)))
        except Exception as e:
            self.events.put(("preflight", (False, "Ошибка проверки", str(e))))

    def _gather_params(self):
        model = self.model_var.get()

        def num(entry, name):
            raw = entry.get().strip().replace(",", ".")
            if raw == "":
                raise ValueError(f"Поле «{name}» не заполнено.")
            return float(raw)

        try:
            params = {
                "model": model,
                "i_nom": SENSOR_MODELS[model],
                "I_start": num(self.e_start, "Начало"),
                "I_stop": num(self.e_stop, "Конец"),
                "I_step": num(self.e_step, "Шаг"),
                "V_limit": num(self.e_vlimit, "Огр. напряжения"),
                "delay": num(self.e_delay, "Задержка установки"),
                "cooling_delay": num(self.e_cool, "Задержка охлаждения"),
                "label": self.e_label.get().strip(),
            }
        except ValueError as e:
            messagebox.showerror("Проверьте параметры", str(e))
            return None

        errors = validate_measure_params(params)
        if errors:
            messagebox.showerror("Некорректные параметры", "\n".join(errors))
            return None
        return params

    def _start_measurement(self):
        if not self._preflight_ok:
            messagebox.showwarning("Проверка не пройдена",
                                   "Измерение недоступно: не пройдена предполётная проверка (NI-VISA/самотесты).")
            return
        params = self._gather_params()
        if params is None:
            return

        self.config_mgr.save(params)
        xlsx_path = make_result_filename(self.data_dir, params["model"], params["label"])

        if not messagebox.askyesno(
            "Запуск измерения",
            f"Датчик: {params['model']} (I ном. {params['i_nom']} А)\n"
            f"Диапазон: {params['I_start']}..{params['I_stop']} А (шаг {params['I_step']} А)\n"
            f"Обе полярности через реле.\n\nЗапустить измерение?",
        ):
            return

        self.stop_event.clear()
        self._set_running(True)
        self._append_log(f"\n=== Измерение: {xlsx_path.name} ===\n")

        addr = {
            "dmm_addr": self.e_dmm.get().strip() or None,
            "src_addr": self.e_src.get().strip() or None,
            "relay_port": self.e_relay.get().strip() or None,
        }
        self.worker = threading.Thread(target=self._measure_worker, args=(params, xlsx_path, addr), daemon=True)
        self.worker.start()

    def _measure_worker(self, params, xlsx_path, addr):
        from visa_backend import make_resource_manager
        from orchestrate import run_measurement_session

        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(self.events)
        rm = None
        try:
            rm = make_resource_manager()
            df = run_measurement_session(
                rm, params, xlsx_path,
                dmm_addr=addr["dmm_addr"], src_addr=addr["src_addr"], relay_port=addr["relay_port"],
                should_stop=self.stop_event.is_set,
            )
            max_err = df['Error_percent'].abs().max()
            mean_err = df['Error_percent'].mean()
            self.events.put(("done", (str(xlsx_path), max_err, mean_err)))
        except Exception as e:
            traceback.print_exc()
            self.events.put(("error", str(e)))
        finally:
            if rm is not None:
                try:
                    rm.close()
                except Exception:
                    pass
            sys.stdout, sys.stderr = old_out, old_err

    def _request_stop(self):
        self.stop_event.set()
        self._append_log("\n… запрошена остановка, завершаю текущую точку/ветвь…\n")
        self.stop_btn.configure(state="disabled")

    def _set_running(self, running):
        self.start_btn.configure(state="disabled" if running else ("normal" if self._preflight_ok else "disabled"))
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _on_close(self):
        self._closing = True
        self.stop_event.set()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
        self.root.destroy()

    def _drain_events(self):
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "preflight":
                    ok, status, detail = payload
                    self._preflight_ok = ok
                    self._set_status(status, "ok" if ok else "error")
                    self.footer_label.configure(
                        text=("Готово к измерению." if ok
                              else "Измерение заблокировано. См. журнал / установите NI-VISA."))
                    self.start_btn.configure(state="normal" if ok else "disabled")
                elif kind == "done":
                    path, max_err, mean_err = payload
                    self._append_log(
                        f"\n✔ Измерение завершено. Данные: {path}\n"
                        f"  Макс. приведённая погрешность: {max_err:.4f} %\n"
                        f"  Средняя приведённая погрешность (со знаком): {mean_err:.4f} %\n"
                    )
                    self._set_running(False)
                    self._set_status("Измерение завершено", "ok")
                elif kind == "error":
                    self._append_log(f"\n✖ Ошибка: {payload}\n")
                    self._set_running(False)
                    self._set_status("Ошибка измерения", "error")
                    messagebox.showerror("Ошибка измерения", payload)
        except queue.Empty:
            pass
        if not self._closing:
            self._after_id = self.root.after(120, self._drain_events)


def launch_gui(args=None) -> int:
    """Точка входа GUI. Возвращает 0 после закрытия окна."""
    root = tk.Tk()
    DTCalGUI(root, args)
    root.mainloop()
    return 0
