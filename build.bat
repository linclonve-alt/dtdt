@echo off
rem Optional local build on a Windows PC. The easy way is GitHub Actions (.github/workflows/build.yml).
pip install fastapi uvicorn requests pyinstaller pywebview tzdata ccxt
set EXTRA=
if exist server.py (
  pip install paradex_py && set "EXTRA=--hidden-import server --collect-all paradex_py --collect-all starknet_py"
)
pyinstaller --noconfirm --onedir --noconsole --name DayTradeRiskManager --add-data "static;static" --hidden-import server_oanda --hidden-import server_ccxt --hidden-import cexlib --hidden-import daylock --hidden-import webview --collect-submodules uvicorn --collect-submodules ccxt --collect-data tzdata --collect-data certifi %EXTRA% trader_app.py
echo Done: dist\DayTradeRiskManager\DayTradeRiskManager.exe
echo Now open installer.iss in Inno Setup and click Compile.
