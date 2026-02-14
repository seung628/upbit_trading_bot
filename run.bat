@echo off
chcp 65001 >nul
cls

echo ==================================
echo 업비트 자동매매 봇 실행 스크립트
echo ==================================
echo.

:: Python 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python이 설치되어 있지 않습니다.
    echo Python 3.8 이상을 설치하세요.
    pause
    exit /b 1
)

echo ✅ Python 버전:
python --version
echo.

:: 패키지 설치
echo 필요한 패키지 설치 중...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo ❌ 패키지 설치 실패
    pause
    exit /b 1
)

echo ✅ 패키지 설치 완료
echo.

:: config.json 확인
if not exist "config.json" (
    echo ⚠️  config.json 파일이 없습니다.
    echo config.example.json을 복사하여 API 키를 설정하세요:
    echo   copy config.example.json config.json
    echo   notepad config.json
    pause
    exit /b 1
)

echo ✅ 설정 파일 확인 완료
echo.

:: logs 디렉토리 생성
if not exist "logs" mkdir logs

echo 🚀 프로그램 시작...
echo.

:: 프로그램 실행
python main.py

pause
