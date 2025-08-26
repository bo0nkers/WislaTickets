# Wisła Tickets Watcher

Automatyczny skrypt zbierający co 10 minut liczbę wolnych miejsc z bilety.wislakrakow.com
i zapisujący je do pliku CSV w repo. Możesz wciągnąć dane do Google Sheets przez =IMPORTDATA().

## Pliki
- scrape_wisla.py – główny skrypt
- requirements.txt – biblioteki do zainstalowania
- .env.sample – przykład konfiguracji (zmień nazwę na .env i uzupełnij)
- .github/workflows/wisla.yml – workflow GitHub Actions, który odpala skrypt co 10 min
