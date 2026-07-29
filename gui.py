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
  - приборы и плата реле определяются только автоматически: полей для
    ручного ввода адресов нет, чтобы оператору не приходилось разбираться
    с VISA-строками и COM-портами (ручные адреса остались во флагах CLI);
  - выбор модели датчика подставляет параметры прохода (0..I_ном, шаг
    I_ном/10 — сетка гарантированно проходит через ноль), поля остаются
    редактируемыми;
  - измерение идёт в отдельном потоке, весь вывод ядра (print) перехватывается
    в журнал; кнопка «Стоп» кооперативно прерывает проход между точками;
  - графиков нет. Результат остаётся в памяти, а запись в .xlsx — отдельная
    кнопка «Сохранить в Excel»: папка запоминается между запусками, имя файла
    предзаполняется и редактируется. Прерванное измерение тоже можно сохранить.
"""
import io
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

from apppaths import default_data_dir
from config import ConfigManager
from cli import make_result_filename, validate_measure_params
from sensors import SENSOR_MODELS


ACCENT = "#2563eb"
ACCENT_ACTIVE = "#1d4ed8"
SAVE_COLOR = "#15803d"
SAVE_ACTIVE = "#166534"
BG = "#f4f5f7"
CARD = "#ffffff"
OK_COLOR = "#15803d"
ERR_COLOR = "#b91c1c"
BUSY_COLOR = "#b45309"
# Приглушённый текст — но не светлее этого: на светлом фоне BG более бледные
# оттенки читаются как «серое на сером».
MUTED = "#4b5563"
# Неактивная кнопка: светлая заливка + тёмная подпись. Прежняя схема (белый
# текст на #9ca3af) сливалась в неразборчивое серое пятно.
DISABLED_BG = "#cbd5e1"
DISABLED_FG = "#475569"

# Значения по умолчанию, не зависящие от модели датчика.
DEFAULT_V_LIMIT = 10.0
DEFAULT_DELAY = 1.0
DEFAULT_COOLING = 1.0

# Шаг = I_ном / STEP_DIVISOR: 11 точек на ветвь (0, 10 … 100 А для ДТ100А1),
# сетка симметрична и содержит ноль, при этом проход не затягивается.
STEP_DIVISOR = 10

# Выше этого числа точек спрашиваем подтверждение: столько времени оператор
# скорее всего не планировал (каждая точка — задержка установки + охлаждения).
POINTS_WARN_THRESHOLD = 100


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

        # Результат последнего измерения живёт в памяти до явного сохранения.
        self.result_df = None
        self.result_params = None
        self.result_saved = False
        self._filename_edited = False

        self.skip_selftest_var = tk.BooleanVar(value=bool(getattr(args, "skip_selftest", False)))
        self.model_var = tk.StringVar(value=next(iter(SENSOR_MODELS)))
        self.save_dir = self.data_dir

        self._closing = False
        self._after_id = None

        self._build_style()
        self._build_ui()
        self._prefill_from_config()
        self.model_var.trace_add("write", self._on_model_change)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.root.after(120, self._drain_events)
        self._run_preflight()

    def _build_style(self):
        self.root.title("DTCal")
        self.root.geometry("880x720")
        self.root.minsize(820, 640)
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
        style.configure("TRadiobutton", background=BG)
        style.configure("TCheckbutton", background=BG)
        style.configure("Accent.TButton", padding=(16, 8), foreground="white",
                        background=ACCENT, font=("Segoe UI Semibold", 10), borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", ACCENT_ACTIVE), ("disabled", DISABLED_BG)],
                  foreground=[("disabled", DISABLED_FG)])
        style.configure("Save.TButton", padding=(16, 14), foreground="white",
                        background=SAVE_COLOR, font=("Segoe UI Semibold", 12), borderwidth=0)
        style.map("Save.TButton",
                  background=[("active", SAVE_ACTIVE), ("disabled", DISABLED_BG)],
                  foreground=[("disabled", DISABLED_FG)])
        style.configure("Danger.TButton", padding=(14, 8))
        style.map("Danger.TButton", foreground=[("disabled", DISABLED_FG)])
        # Подпись-статус внизу окна несёт важную информацию, поэтому у неё
        # отдельные стили: обычный тёмный и явно красный при блокировке.
        style.configure("Footer.TLabel", background=BG, foreground="#374151")
        style.configure("FooterErr.TLabel", background=BG, foreground=ERR_COLOR,
                        font=("Segoe UI Semibold", 10))

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
        body.columnconfigure(0, minsize=350)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_params(body)
        self._build_right(body)

        footer = ttk.Frame(self.root, padding=(18, 6, 18, 12))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.footer_label = ttk.Label(footer, text="", style="Footer.TLabel")
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
        ttk.Label(mdl, text="Выбор модели подставляет параметры прохода ниже.",
                  style="Sub.TLabel").grid(row=len(SENSOR_MODELS), column=0, sticky="w", pady=(6, 0))

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

        ttk.Label(pf, text="Обе полярности снимаются автоматически через реле.",
                  style="Sub.TLabel").grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

        actions = ttk.Frame(left)
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
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
                        command=self._run_preflight).grid(row=3, column=0, sticky="w", pady=(8, 0))

    def _param_row(self, parent, row, label, unit=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent, width=12)
        entry.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 6))
        ttk.Label(parent, text=unit, style="Muted.TLabel", width=3).grid(row=row, column=2, sticky="w", pady=3)
        return entry

    def _build_right(self, parent):
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.log = scrolledtext.ScrolledText(right, wrap="word", height=10,
                                             font=("Consolas", 9), bg="#0f172a", fg="#e2e8f0",
                                             insertbackground="#e2e8f0", relief="flat", padx=10, pady=8)
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")

        self._build_save_block(right)

    def _build_save_block(self, parent):
        box = ttk.Labelframe(parent, text="Результат", padding=10)
        box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Папка").grid(row=0, column=0, sticky="w", pady=3)
        self.dir_label = ttk.Label(box, text=self._short_path(self.save_dir), style="Muted.TLabel")
        self.dir_label.grid(row=0, column=1, sticky="w", padx=(8, 6), pady=3)
        ttk.Button(box, text="Выбрать…", command=self._choose_dir).grid(row=0, column=2, sticky="e", pady=3)

        ttk.Label(box, text="Имя файла").grid(row=1, column=0, sticky="w", pady=3)
        self.e_filename = ttk.Entry(box)
        self.e_filename.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=3)
        # Правку имени оператором уважаем: автоподстановка её больше не перетирает.
        self.e_filename.bind("<KeyRelease>", self._on_filename_edit)

        self.save_btn = ttk.Button(box, text="💾  Сохранить в Excel", style="Save.TButton",
                                   command=self._save_excel, state="disabled")
        self.save_btn.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.save_hint = ttk.Label(box, text="Данных пока нет — сначала выполните измерение.",
                                   style="Sub.TLabel")
        self.save_hint.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Контекстное меню на большой кнопке: выбор/открытие папки сохранения.
        self.save_menu = tk.Menu(self.root, tearoff=0)
        self.save_menu.add_command(label="Выбрать папку…", command=self._choose_dir)
        self.save_menu.add_command(label="Открыть папку", command=self._open_dir)
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.save_btn.bind(seq, self._popup_save_menu)

    def _popup_save_menu(self, event):
        try:
            self.save_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.save_menu.grab_release()

    # --------------------------------------------------------------- параметры
    def _model_defaults(self, model):
        i_nom = SENSOR_MODELS[model]
        return {
            "I_start": 0.0,
            "I_stop": i_nom,
            "I_step": i_nom / STEP_DIVISOR,
        }

    @staticmethod
    def _set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, f"{value:g}" if isinstance(value, (int, float)) else str(value))

    def _apply_model_defaults(self, model):
        """Подставляет зависящие от модели параметры прохода."""
        defaults = self._model_defaults(model)
        self._set_entry(self.e_start, defaults["I_start"])
        self._set_entry(self.e_stop, defaults["I_stop"])
        self._set_entry(self.e_step, defaults["I_step"])

    def _on_model_change(self, *_):
        model = self.model_var.get()
        if model not in SENSOR_MODELS:
            return
        # Ток зависит от модели — пересчитываем. Задержки и ограничение
        # напряжения от модели не зависят, их правки оператора не трогаем.
        self._apply_model_defaults(model)
        self._refresh_filename()

    def _prefill_from_config(self):
        saved = self.config_mgr.load() or {}

        model = saved.get("model")
        if model in SENSOR_MODELS:
            self.model_var.set(model)
        else:
            model = self.model_var.get()

        self._apply_model_defaults(model)
        self._set_entry(self.e_vlimit, DEFAULT_V_LIMIT)
        self._set_entry(self.e_delay, DEFAULT_DELAY)
        self._set_entry(self.e_cool, DEFAULT_COOLING)

        # Сохранённые значения перекрывают подстановку по умолчанию.
        mapping = {
            "I_start": self.e_start, "I_stop": self.e_stop, "I_step": self.e_step,
            "V_limit": self.e_vlimit, "delay": self.e_delay, "cooling_delay": self.e_cool,
            "label": self.e_label,
        }
        for key, entry in mapping.items():
            val = saved.get(key)
            if val is not None:
                self._set_entry(entry, val)

        saved_dir = saved.get("save_dir")
        if saved_dir:
            candidate = Path(saved_dir)
            if candidate.is_dir():
                self.save_dir = candidate
        self.dir_label.configure(text=self._short_path(self.save_dir))
        self._refresh_filename()

    @staticmethod
    def _short_path(path, max_len: int = 46) -> str:
        """Длинный путь в подписи не должен растягивать окно — обрезаем слева."""
        text = str(path)
        return text if len(text) <= max_len else "…" + text[-(max_len - 1):]

    def _on_filename_edit(self, _event=None):
        self._filename_edited = True

    def _refresh_filename(self):
        """Подставляет имя файла по текущей модели и комментарию, если оператор его не правил."""
        if getattr(self, "e_filename", None) is None:
            return
        if getattr(self, "_filename_edited", False):
            return
        model = self.model_var.get()
        label = self.e_label.get().strip() if self.e_label.get() else ""
        suggested = make_result_filename(self.save_dir, model, label).name
        self._set_entry(self.e_filename, suggested)

    def _gather_params(self):
        model = self.model_var.get()

        def num(entry, name):
            raw = entry.get().strip().replace(",", ".")
            if raw == "":
                raise ValueError(f"Поле «{name}» не заполнено.")
            try:
                return float(raw)
            except ValueError:
                raise ValueError(f"Поле «{name}»: «{raw}» не похоже на число.")

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

        # Мягкие предупреждения: параметры формально корректны, но оператор,
        # скорее всего, ошибся.
        points = (int(round((params["I_stop"] - params["I_start"]) / params["I_step"])) + 1) * 2
        if params["I_stop"] > params["i_nom"]:
            if not messagebox.askyesno(
                "Выход за номинал",
                f"Конечный ток {params['I_stop']:g} А больше номинального "
                f"{params['i_nom']:g} А для {model}.\n"
                "Датчик будет измеряться вне рабочего диапазона.\n\nПродолжить?",
            ):
                return None
        if points > POINTS_WARN_THRESHOLD:
            approx_min = points * (params["delay"] + params["cooling_delay"]) / 60.0
            if not messagebox.askyesno(
                "Много точек",
                f"Получается {points} точек (обе полярности), это примерно "
                f"{approx_min:.0f} мин только на задержки.\n\nПродолжить?",
            ):
                return None
        return params

    # --------------------------------------------------------------- preflight
    def _run_preflight(self):
        self._preflight_ok = False
        self.start_btn.configure(state="disabled")
        self._set_status("Проверка NI-VISA и самотестов…", "busy")
        self.footer_label.configure(text="Идёт предполётная проверка…", style="Footer.TLabel")
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

    # --------------------------------------------------------------- измерение
    def _start_measurement(self):
        if not self._preflight_ok:
            messagebox.showwarning("Проверка не пройдена",
                                   "Измерение недоступно: не пройдена предполётная проверка (NI-VISA/самотесты).")
            return
        if self.worker is not None and self.worker.is_alive():
            return

        if self.result_df is not None and not self.result_saved:
            if not messagebox.askyesno(
                "Несохранённые данные",
                "Результат предыдущего измерения не сохранён в Excel и будет потерян.\n\nПродолжить?",
            ):
                return

        params = self._gather_params()
        if params is None:
            return

        if not messagebox.askyesno(
            "Запуск измерения",
            f"Датчик: {params['model']} (I ном. {params['i_nom']:g} А)\n"
            f"Диапазон: {params['I_start']:g}..{params['I_stop']:g} А (шаг {params['I_step']:g} А)\n"
            f"Обе полярности через реле.\n\nЗапустить измерение?",
        ):
            return

        self._save_config(params)

        self.stop_event.clear()
        self.result_df = None
        self.result_params = None
        self.result_saved = False
        self._set_running(True)
        self._update_save_state()
        self._append_log(f"\n=== Измерение: {params['model']} ===\n")

        self.worker = threading.Thread(target=self._measure_worker, args=(params,), daemon=True)
        self.worker.start()

    def _measure_worker(self, params):
        from visa_backend import make_resource_manager
        from orchestrate import run_measurement_session

        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(self.events)
        rm = None
        try:
            rm = make_resource_manager()
            # xlsx_path=None: файл пишется отдельной кнопкой «Сохранить в Excel».
            df = run_measurement_session(
                rm, params, xlsx_path=None,
                should_stop=self.stop_event.is_set,
            )
            self.events.put(("done", (df, params)))
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

    # --------------------------------------------------------------- сохранение
    def _save_config(self, params=None):
        data = dict(params) if params else (self.config_mgr.load() or {})
        data["save_dir"] = str(self.save_dir)
        try:
            self.config_mgr.save(data)
        except Exception as e:
            self._append_log(f"Предупреждение: не удалось сохранить конфиг: {e}\n")

    def _choose_dir(self):
        chosen = filedialog.askdirectory(
            title="Куда сохранять результаты измерений",
            initialdir=str(self.save_dir if self.save_dir.is_dir() else self.data_dir),
        )
        if not chosen:
            return
        self.save_dir = Path(chosen)
        self.dir_label.configure(text=self._short_path(self.save_dir))
        self._save_config()
        self._append_log(f"Папка сохранения: {self.save_dir}\n")

    def _open_dir(self):
        target = self.save_dir if self.save_dir.is_dir() else self.data_dir
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(target))  # noqa: S606 — открытие своей же папки
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as e:
            messagebox.showerror("Не удалось открыть папку", str(e))

    @staticmethod
    def _points_word(n: int) -> str:
        """Согласование числительного: 1 точка, 2-4 точки, 5+ точек."""
        if n % 100 in range(11, 20):
            return "точек"
        last = n % 10
        if last == 1:
            return "точка"
        if last in (2, 3, 4):
            return "точки"
        return "точек"

    def _update_save_state(self):
        has_data = self.result_df is not None and not self.result_df.empty
        self.save_btn.configure(state="normal" if has_data else "disabled")
        if not has_data:
            self.save_hint.configure(text="Данных пока нет — сначала выполните измерение.")
        elif self.result_saved:
            self.save_hint.configure(text="Сохранено.")
        else:
            n = len(self.result_df)
            self.save_hint.configure(
                text=f"Готово к сохранению: {n} {self._points_word(n)}. "
                     f"Правый клик по кнопке — выбор папки.")

    def _save_excel(self):
        if self.result_df is None or self.result_df.empty:
            messagebox.showwarning("Нет данных", "Сохранять нечего: измерение не дало ни одной точки.")
            return

        name = self.e_filename.get().strip()
        if not name:
            self._refresh_filename()
            name = self.e_filename.get().strip()
        if not name.lower().endswith(".xlsx"):
            name += ".xlsx"

        # Защита от случайных разделителей пути в поле имени.
        name = Path(name).name

        target_dir = self.save_dir
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Папка недоступна",
                                 f"Не удалось использовать папку:\n{target_dir}\n\n{e}\n\n"
                                 "Выберите другую папку.")
            return

        path = target_dir / name
        if path.exists() and not messagebox.askyesno(
            "Файл существует", f"Файл уже есть:\n{path}\n\nПерезаписать?"
        ):
            return

        from report import write_report_xlsx
        try:
            write_report_xlsx(path, self.result_df, self.result_params)
        except PermissionError:
            messagebox.showerror(
                "Файл занят",
                f"Не удалось записать:\n{path}\n\n"
                "Скорее всего файл открыт в Excel — закройте его и повторите. "
                "Данные измерения не потеряны.")
            return
        except Exception as e:
            messagebox.showerror("Ошибка сохранения",
                                 f"Не удалось записать файл:\n{e}\n\nДанные измерения не потеряны.")
            return

        self.result_saved = True
        self._update_save_state()
        self._save_config()
        self._append_log(f"\n💾 Сохранено: {path}\n")
        self._set_status("Данные сохранены", "ok")

    # --------------------------------------------------------------- служебное
    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text, kind="muted"):
        color = {"ok": OK_COLOR, "error": ERR_COLOR, "busy": BUSY_COLOR}.get(kind, MUTED)
        self.status_label.configure(text=text, foreground=color)
        self.status_dot.itemconfigure(self._dot, fill=color)

    def _on_close(self):
        if self.result_df is not None and not self.result_saved and not self.result_df.empty:
            if not messagebox.askyesno(
                "Несохранённые данные",
                "Результат измерения не сохранён в Excel.\n\nВыйти и потерять данные?",
            ):
                return
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(
                "Измерение идёт",
                "Измерение ещё выполняется. Закрыть программу?\n"
                "Источник и реле будут выключены при завершении текущей точки.",
            ):
                return
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
                              else "Измерение заблокировано. См. журнал / установите NI-VISA."),
                        style="Footer.TLabel" if ok else "FooterErr.TLabel")
                    self.start_btn.configure(state="normal" if ok else "disabled")
                elif kind == "done":
                    df, params = payload
                    self.result_df = df
                    self.result_params = params
                    self.result_saved = False
                    self._set_running(False)
                    self._refresh_filename()
                    self._update_save_state()
                    if df is None or df.empty:
                        self._append_log("\n⚠ Измерение не дало ни одной точки (остановлено сразу?).\n")
                        self._set_status("Нет данных", "error")
                    else:
                        errors = df['Error_percent'].dropna()
                        n = len(df)
                        if errors.empty:
                            self._append_log(
                                f"\n✔ Измерение завершено: {n} {self._points_word(n)}, "
                                "но ни одно чтение не удалось (все точки NaN).\n")
                            self._set_status("Измерение завершено, данных нет", "error")
                        else:
                            self._append_log(
                                f"\n✔ Измерение завершено: {n} {self._points_word(n)}.\n"
                                f"  Макс. приведённая погрешность: {errors.abs().max():.4f} %\n"
                                f"  Средняя приведённая погрешность (со знаком): {errors.mean():+.4f} %\n"
                                "  Нажмите «Сохранить в Excel».\n")
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
