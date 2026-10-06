@echo off
cd /d "%~dp0"
title Job Track 投递追踪表

echo ============================================
echo   Job Track 秋招投递追踪表（Skill 分发版）
echo   - 数据只保存在本机 html 文件里，不上传
echo   - 首次使用请点页面右上角「设置」，填邮箱授权码 + AI Key
echo ============================================

rem ---- 若本服务已在 8788-8799 运行，直接打开浏览器，不重复启动 ----
set "EXISTING_PORT="
where curl >nul 2>nul
if errorlevel 1 goto skipreuse
for /L %%p in (8788,1,8799) do (
  curl.exe -s --max-time 1 "http://localhost:%%p/" | findstr /c:"AUTUMN RECRUITMENT" >nul 2>nul
  if not errorlevel 1 (set "EXISTING_PORT=%%p" & goto reusehit)
)
:skipreuse
if defined EXISTING_PORT goto reusehit

:startfresh

rem ---- 自动寻找 Python（python / py -3），并校验可用 ----
set "PYCMD="
where python >nul 2>nul
if errorlevel 1 goto trypy
python -c "import sys" >nul 2>nul
if errorlevel 1 goto trypy
set "PYCMD=python"
goto pyfound
:trypy
where py >nul 2>nul
if errorlevel 1 goto probe_local
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto probe_local
set "PYCMD=py -3"
goto pyfound

:probe_local
rem ---- u81eau52a8u63a2u6d4bu672cu673au5e38u89c1 Python u5b89u88c5u8defu5f84uff08u4e0du4f9du8d56 PATHuff09----
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
  if exist "%%d\python.exe" (
    "%%d\python.exe" -c "import sys" >nul 2>nul
    if not errorlevel 1 (set "PYCMD=%%d\python.exe" & goto pyfound)
  )
)
goto nopython
:pyfound

rem ---- 找一个空闲端口（默认 8788，占用则往上找，8788-8799）----
set "PORT=8788"
:findport
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>nul
if errorlevel 1 goto portok
set /a PORT+=1
if %PORT% gtr 8799 goto portfail
goto findport

:portok
echo 使用端口：%PORT%
echo 正在启动，浏览器将自动打开 http://localhost:%PORT%
rem 2 秒后自动打开浏览器（等服务就绪）
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:%PORT%"
rem 前台运行（黑窗口就是服务窗口，Ctrl+C 停止）
%PYCMD% server.py %PORT%
echo.
echo 服务已停止，可以关闭本窗口。
pause
exit /b 0

:reusehit
echo 检测到服务已在运行：http://localhost:%EXISTING_PORT%
echo 直接打开浏览器（不重复启动）。
start http://localhost:%EXISTING_PORT%
exit /b 0

:nopython
echo [错误] 没有找到 Python。
echo 请先安装 Python 3.10 或更高版本: https://www.python.org/downloads/
echo 安装时务必勾选 "Add Python to PATH"。
pause
exit /b 1

:portfail
echo [错误] 8788-8799 端口全被占用，无法启动。
echo 请关闭占用端口的程序后重试。
pause
exit /b 1
