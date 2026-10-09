@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo 未找到 Python，请先安装 Python 3 并勾选 Add to PATH。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境并安装依赖...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

echo.
echo 局域网云盘启动中...
echo 本机访问: http://127.0.0.1:8091
echo 局域网请用本机 IP + 8091 端口访问
echo 共享密码见 config.json
echo.
python app.py
pause
