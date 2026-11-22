# Развёртывание SoftX на s0ftx.ru

Flask обслуживает интерфейс и API через Gunicorn. Один процесс Gunicorn запускает очередь обработки PDF и подхватывает незавершённые задания при перезапуске. Caddy принимает соединения HTTPS. У каждого профиля браузера своя база SQLite и каталог PDF в `SOFTX_DATA/workspaces/`. Доступ без регистрации привязан к cookie. При очистке cookie или переходе в другой профиль браузер получит новую пустую библиотеку.

1. На VPS с systemd установите Python 3.11+, `python3-venv`, `mupdf-tools` и Caddy. Разместите [серверный репозиторий](https://github.com/SaSitme/softx-science) в `/opt/softx`, а [клиентский](https://github.com/SaSitme/softx-science-client) в `/opt/softx-client`. Добавьте `SOFTX_STATIC_DIR=/opt/softx-client` в `/etc/softx.env`, затем установите зависимости.

   ```sh
   cd /opt/softx
   python3 -m venv .venv
   .venv/bin/pip install -r deploy/requirements.txt
   sudo install -m 644 deploy/softx.service /etc/systemd/system/softx.service
   sudo systemctl daemon-reload
   ```

2. Для переноса серверного хранилища скопируйте дерево `/var/lib/private/softx` с сохранением каталогов рабочих областей и их имён. На время снимка приостановите загрузки и правки либо снимите каждую SQLite через SQLite Backup API. Прежняя общая база в корне каталога хранится отдельно от личных библиотек. Ключи API настраиваются отдельно.

3. Запустите службу и проверьте интерфейс изнутри VPS.

   ```sh
   sudo systemctl enable --now softx
   curl --fail http://127.0.0.1:8000/ -o /dev/null
   ```

4. Добавьте сайт из `deploy/Caddyfile` в действующую конфигурацию Caddy и сохраните остальные сайты VPS. Убедитесь, что DNS указывает на VPS, а порты 80 и 443 доступны. Caddy получит сертификат автоматически.

5. Откройте [s0ftx.ru](https://s0ftx.ru) в приватном окне и проверьте библиотеку, оригинал PDF и загрузку тестового документа.

## Локальная модель и OCR

Поместите Linux-сборку llama.cpp с библиотеками в `/opt/softx-model/bin`, а GGUF-модель в `/opt/softx-model/model.gguf`. Установите `deploy/softx-model.service` в `/etc/systemd/system/` и запустите службу. Она принимает запросы приложения на `127.0.0.1:8090`. Установите `tesseract-ocr`, `tesseract-ocr-eng` и `tesseract-ocr-rus` для распознавания английских и русских сканов. Добавьте настройки в `/etc/softx.env` и перезапустите SoftX.

```ini
SOFTX_LOCAL_ONLY=1
SOFTX_LOCAL_AI_PORT=8090
SOFTX_OCR=1
SOFTX_OCR_LANG=eng+rus
```
