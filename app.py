#!/usr/bin/env python
"""
Единая точка входа DTCal (используется и при сборке в exe).

  DTCal.exe              -> графический интерфейс (GUI)
  DTCal.exe gui          -> то же самое
  DTCal.exe measure ...  -> измерение из командной строки

Вся логика диспетчеризации живёт в run.main; здесь только тонкая обёртка,
чтобы у exe было осмысленное имя точки входа.
"""
import sys

from run import main

if __name__ == "__main__":
    sys.exit(main())
