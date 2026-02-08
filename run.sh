#!/bin/bash

echo "=================================="
echo "업비트 자동매매 봇 실행 스크립트"
echo "=================================="

# Python 버전 확인
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3가 설치되어 있지 않습니다."
    exit 1
fi

echo "✅ Python 버전: $(python3 --version)"

# 패키지 설치 확인
echo ""
echo "필요한 패키지 설치 중..."
pip3 install -q -r requirements.txt

if [ $? -ne 0 ]; then
    echo "❌ 패키지 설치 실패"
    exit 1
fi

echo "✅ 패키지 설치 완료"

# config.json 존재 확인
if [ ! -f "config.json" ]; then
    echo ""
    echo "⚠️  config.json 파일이 없습니다."
    echo "config.example.json을 복사하여 API 키를 설정하세요:"
    echo "  cp config.example.json config.json"
    echo "  nano config.json"
    exit 1
fi

echo "✅ 설정 파일 확인 완료"

# logs 디렉토리 생성
mkdir -p logs

echo ""
echo "🚀 프로그램 시작..."
echo ""

# 메인 프로그램 실행
python3 main.py

