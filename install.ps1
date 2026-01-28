# Скрипт установки зависимостей для CareerAI Bot
# Использует только готовые wheels (без компиляции)

Write-Host "🔍 Проверка Python версии..." -ForegroundColor Cyan
python --version

Write-Host "`n📦 Обновление pip..." -ForegroundColor Cyan
python -m pip install --upgrade pip

Write-Host "`n🧹 Очистка кэша pip..." -ForegroundColor Cyan
pip cache purge

Write-Host "`n📥 Установка зависимостей (только готовые wheels)..." -ForegroundColor Cyan
Write-Host "Если какой-то пакет не установится, попробуем установить его отдельно.`n" -ForegroundColor Yellow

# Пробуем установить с предпочтением бинарных пакетов
pip install --prefer-binary --no-cache-dir -r requirements.txt

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n⚠️  Установка с --prefer-binary не удалась. Пробуем установить пакеты по одному..." -ForegroundColor Yellow
    
    $packages = @(
        "aiogram>=3.13.0",
        "fastapi>=0.109.0",
        "httpx>=0.26.0",
        "uvicorn[standard]>=0.27.0",
        "python-multipart>=0.0.6",
        "PyPDF2>=3.0.0",
        "python-dotenv>=1.0.0"
    )
    
    foreach ($pkg in $packages) {
        Write-Host "`n📦 Устанавливаю: $pkg" -ForegroundColor Cyan
        pip install --prefer-binary --no-cache-dir $pkg
        if ($LASTEXITCODE -ne 0) {
            Write-Host "❌ Ошибка при установке: $pkg" -ForegroundColor Red
            Write-Host "Попробуйте установить вручную или используйте Python 3.11/3.12" -ForegroundColor Yellow
        }
    }
}

Write-Host "`n✅ Проверка установленных пакетов..." -ForegroundColor Green
pip list | Select-String -Pattern "aiogram|fastapi|httpx|uvicorn|PyPDF2|python-dotenv|multipart"

Write-Host ""
Write-Host "✨ Готово! Теперь можно запустить бота:" -ForegroundColor Green
Write-Host "   python careerai_bot_mvp.py" -ForegroundColor Cyan
