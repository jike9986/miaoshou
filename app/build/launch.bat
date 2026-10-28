@echo off
rem ===========================================================================
rem Aohuang Desktop launcher
rem
rem Why this file exists:
rem   On this machine the Electron sandbox aborts (0xC0000005) during native
rem   init - BEFORE any JavaScript runs. Verified by writing a log on the very
rem   first line of m.js: launching electron with no arguments produced no log
rem   file at all. Adding only --user-data-dir still crashed, so userData is
rem   not the cause. Adding only --no-sandbox reached app ready normally.
rem
rem   Therefore --no-sandbox has to be present on the command line from the
rem   first instant, which app.commandLine.appendSwitch() cannot do for a
rem   plain double-click. Double-click this file instead of the exe.
rem
rem   Everything user-visible (shortcut name, window title, taskbar) is still
rem   the Chinese product name; only this launcher is ASCII because cmd.exe
rem   mis-decodes multibyte content on this machine.
rem ===========================================================================
cd /d "%~dp0"
start "" "%~dp0AohuangShangpin.exe" --no-sandbox %*
