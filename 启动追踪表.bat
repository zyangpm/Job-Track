@echo off
cd /d "%~dp0"
title 秋招投递追踪表

echo ============================================
echo   [Skill 分发版] 新用户空表；你的个人数据在原版 job_track 文件夹
echo   秋招投递追踪表 - 启动中...
echo ============================================

rem ---- 如果服务已经在跑（8768-8790 任意一个端口），直接打开浏览器，不开第二个 ----
set "EXISTING_PORT="
where curl >nul 2>nul
if errorlevel 1 goto skipreuse
for /L %%p in (8768,1,8790) do (
  curl.exe -s --max-time 1 "http://localhost:%%p/" | findstr /c:"AUTUMN RECRUITMENT" >nul 2>nul
  if not errorlevel 1 (set "EXISTING_PORT=%%p" & goto reusehit)
)
if defined EXISTING_PORT goto reusehit
goto skipreuse

:reusehit
echo 检测到服务已在运行：http://localhost:%EXISTING_PORT%
echo 直接打开浏览器（请关掉其它旧标签页，避免看到旧数据）。
start http://localhost:%EXISTING_PORT%
exit /b 0

:skipreuse

rem ---- 自动寻找 Python：python → py -3，逐个验证真能跑 ----
set "PYCMD="
where python >nul 2>nul
if errorlevel 1 goto trypy
python -c "import sys" >nul 2>nul
if errorlevel 1 goto trypy
set "PYCMD=python"
goto pyfound
:trypy
where py >nul 2>nul
if errorlevel 1 goto nopython
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto nopython
set "PYCMD=py -3"
:pyfound

rem ---- 找一个空闲端口：默认 8768，被占用就往后找（8768-8790）----
set "PORT=8768"
:findport
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>nul
if errorlevel 1 goto portok
set /a PORT+=1
if %PORT% gtr 8790 goto portfail
goto findport

:portok
echo 使用端口：%PORT%
rem 2 秒后自动打开浏览器（后台执行）
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:%PORT%"
rem 前台启动服务：这个窗口就是服务窗口，按 Ctrl+C 可停止
%PYCMD% server.py %PORT%
echo.
echo 服务已停止，按任意键关闭窗口。
pause
exit /b 0

:nopython
echo [错误] 没有找到 Python。
echo 请先安装 Python 3.10 或更高版本: https://www.python.org/downloads/
echo 安装时务必勾选 "Add Python to PATH"。
pause
exit /b 1

:portfail
echo [错误] 8768-8790 端口全部被占用，无法启动。
echo 请关闭占用端口的程序后重试。
pause
exit /b 1
