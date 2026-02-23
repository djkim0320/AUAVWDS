$ErrorActionPreference = 'Stop'
Write-Host "[1/3] Installing Python dependencies..."
python -m pip install -U pip pyinstaller -r backend/requirements.txt
Write-Host "[2/3] Building backend.exe with PyInstaller..."
Push-Location backend
python -m PyInstaller backend.spec --noconfirm
Pop-Location
Write-Host "[3/3] backend.exe ready: backend/dist/backend/backend.exe"

