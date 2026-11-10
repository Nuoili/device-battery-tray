@echo off
chcp 936 >nul
setlocal
title Device Battery 卸载器

set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "DATADIR=%APPDATA%\Device Battery"
set "OLDDATADIR=%APPDATA%\DeviceBatteryTray"

echo ==============================================
echo   Device Battery 卸载器
echo ==============================================
echo.
echo   程序目录：%APPDIR%
echo   数据目录：%DATADIR%
echo.

rem ---- 防误删：本卸载器会把「自己所在的文件夹」整个送进回收站，
rem      必须先确认这确实是本程序目录，否则一律中止 ----
set "OKDIR="
for %%f in ("%APPDIR%\*.exe") do (
    echo %%~nf | findstr /i /c:"Device Battery" >nul && set "OKDIR=1"
    echo %%~nf | findstr /i /c:"BatteryTray" >nul && set "OKDIR=1"
    echo %%~nf | findstr /c:"设备电量" >nul && set "OKDIR=1"
)
if exist "%APPDIR%\源码\battery_tray.py" set "OKDIR=1"
if not defined OKDIR (
    echo   [中止] 这个文件夹看起来不是 Device Battery 的程序目录：
    echo          %APPDIR%
    echo.
    echo   为了避免误删别的文件夹，本卸载器不会继续。
    echo   请把它放回程序目录（和 Device Battery.exe 同一个文件夹）后再运行。
    echo   或者手动卸载：删掉 exe，再删 %DATADIR% 文件夹。
    echo.
    ping -n 8 127.0.0.1 >nul
    exit /b 1
)

echo   本操作将：
echo     1. 结束 Device Battery 进程（含旧版改名前后的 exe）
echo     2. 移除开机自启（HKCU Run 键 Device Battery，以及旧键名 DeviceBatteryTray）
echo     3. 删除桌面与开始菜单里的 Device Battery 快捷方式
echo     4. 将上面的程序目录整个移入回收站
echo.
echo   代码已备份在 GitHub（Nuoili/device-battery-tray），可随时找回。
echo.
choice /c YN /m "确认卸载吗 [Y=卸载 / N=取消]"
if errorlevel 2 goto :cancel

rem ---- 1. 结束进程：Release 里 exe 可能被改名，把新旧常见名字都清一遍 ----
taskkill /f /im "Device Battery.exe" >nul 2>&1
taskkill /f /im "设备电量.exe" >nul 2>&1
taskkill /f /im "BatteryTray.exe" >nul 2>&1
taskkill /f /im "DeviceBatteryTray.exe" >nul 2>&1

rem ---- 2. 移除开机自启（只删值，不删整个 Run 键）----
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "Device Battery" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v DeviceBatteryTray /f >nul 2>&1

rem ---- 3. 快捷方式（桌面 / 开始菜单）----
del /f /q "%USERPROFILE%\Desktop\Device Battery.lnk" >nul 2>&1
del /f /q "%PUBLIC%\Desktop\Device Battery.lnk" >nul 2>&1
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Device Battery.lnk" >nul 2>&1
del /f /q "%ProgramData%\Microsoft\Windows\Start Menu\Programs\Device Battery.lnk" >nul 2>&1

rem ---- 4. 用户数据：默认保留，明确询问 ----
if exist "%DATADIR%" goto :ask_data
if exist "%OLDDATADIR%" set "DATADIR=%OLDDATADIR%"
if exist "%OLDDATADIR%" goto :ask_data
goto :drop_dir

:ask_data
echo.
choice /c YN /n /m "保留用户数据（配置与电量历史）吗？[Y=保留 / N=删除到回收站]"
if errorlevel 2 call :drop_data

:drop_dir
rem ---- 5. 程序目录整个进回收站（延迟等本窗口释放文件句柄）----
set "UNINSTALL_DIR=%APPDIR%"
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 3; Add-Type -AssemblyName Microsoft.VisualBasic; $d=$env:UNINSTALL_DIR; foreach($i in 1..6){ try{ [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($d,'OnlyErrorDialogs','SendToRecycleBin'); break }catch{ Start-Sleep 2 } }"

echo.
echo   进程已结束、自启与快捷方式已移除。
echo   程序目录即将移入回收站，本窗口可关闭。
echo   （若 %APPDIR% 仍存在，说明有文件被占用，稍后重跑本卸载器即可）
ping -n 5 127.0.0.1 >nul
exit /b 0

:drop_data
set "DROP_DATA=%DATADIR%"
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 1; Add-Type -AssemblyName Microsoft.VisualBasic; $d=$env:DROP_DATA; foreach($i in 1..5){ try{ [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($d,'OnlyErrorDialogs','SendToRecycleBin'); break }catch{ Start-Sleep 1 } }"
echo   用户数据已送进回收站。
exit /b 0

:cancel
echo   已取消，未做任何更改。
ping -n 3 127.0.0.1 >nul
exit /b 0
