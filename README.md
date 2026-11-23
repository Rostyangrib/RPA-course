# RPA-course

RPA-робот «ОфисСнаб» для расчёта стоимости заказа.

## Ручной запуск

В PowerShell из корня проекта с Python 3.11 или новее:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m rpa_course
```

Команда проверяет `data/price.xlsx`, сохраняет его снимок в `.runtime/state.sqlite3` и выводит число товаров и SHA-256. При ошибке цикл завершается с кодом `1` и объяснением в стандартном потоке ошибок.

Пути задаются в `config.toml`. Чтобы выбрать другой файл, передайте `--config путь.toml`. Относительные пути внутри TOML считаются от папки этого файла.

## Подключение учебного ящика Яндекс Почты

1. Включите IMAP и пароли приложений в настройках учебного ящика. Создайте пароль приложения для почты. [Настройки почтовых клиентов Яндекса](https://yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/mail-clients-troubleshooting.html)
2. Укажите адрес ящика в `mail.address` в `config.toml`.
3. Сохраните пароль приложения в диспетчере учётных данных Windows. Подставьте тот же адрес в команду, пароль введите по запросу.

   ```powershell
   .\.venv\Scripts\python.exe -m keyring -b keyring.backends.Windows.WinVaultKeyring set RPA-course ИМЯ@yandex.ru
   ```

4. Запустите робота. Первый запуск запомнит границу существующих писем. Отправьте на учебный ящик письмо с темой «Расчёт заказа» после этого запуска и запустите робота ещё раз.

Результат приёма сохраняется в SQLite. Письма читаются без отметки «Прочитано». Пустое значение `mail.address` оставляет доступной проверку прайса без подключения к почте.

## Сопоставление товаров через GigaChat

В `config.toml` заданы модель `GigaChat-2-Pro`, область доступа `GIGACHAT_API_PERS`, URL API и путь к PEM-файлу. Клиент использует проверку TLS. [Справка по сертификатам](https://developers.sber.ru/docs/ru/gigachat/certificates)

Сохраните ключ авторизации без префикса `Basic` в диспетчере учётных данных Windows. Вставьте ключ по запросу команды.

```powershell
.\.venv\Scripts\python.exe -m keyring -b keyring.backends.Windows.WinVaultKeyring set RPA-course GIGACHAT_CREDENTIALS
```

Проверить GigaChat на вымышленном заказе из ТЗ можно отдельно от почты. Для учебного прайса ожидаемый итог — `2790.00`.

```powershell
.\.venv\Scripts\python.exe -m rpa_course --check-gigachat
```

При обычном запуске робот анализирует зарегистрированные обращения и сохраняет расчёт или причины уточнения в `order_analyses` вместе с контрольной суммой прайса. В GigaChat передаются текст запроса и характеристики товаров. Цены робот берёт из Excel, суммы рассчитывает локально.

## Ответы клиентам

После расчёта робот отправляет КП или письмо-уточнение через `smtp.yandex.ru:465` с TLS и тем же паролем приложения. Ответ отправляется одному адресату в исходной цепочке. КП нумеруются по иркутской дате и действуют пять календарных дней. [Параметры SMTP Яндекса](https://yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/mail-clients-troubleshooting.html)

До отправки письмо сохраняется как `.eml` в папке `responses` рядом с базой состояний. В `responses` базы хранятся номер КП, дата, путь к копии и код ошибки. При неясном результате SMTP или прерванной отправке робот сохраняет статус `send_unconfirmed`. Сотрудник проверяет, был ли принят ответ, и при необходимости отвечает вручную. Обращения без корректного `Message-ID` помечаются для проверки.

Шаблоны HTML и обычного текста находятся в `rpa_course/templates`. Проверки с имитацией IMAP, GigaChat и SMTP запускаются командой `python -m unittest discover -s tests`.

[План реализации](PLAN.md)
