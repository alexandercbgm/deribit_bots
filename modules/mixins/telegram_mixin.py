import yaml
import json
import requests

TELEGRAM_CONFIG_FILE = "config/telegram_config.yaml"  # Глобальная переменная для имени конфигурационного файла

class TelegramMixin:
    def __init__(self):
        """
        Инициализация TelegramMixin.
        Загружает параметры из конфигурационного файла.
        """
        with open(TELEGRAM_CONFIG_FILE, "r") as file:
            config = yaml.safe_load(file)

        # Проверка наличия необходимых ключей в конфигурации
        if "chat_name" not in config or "bot_token" not in config:
            raise ValueError(f"{TELEGRAM_CONFIG_FILE} must include 'chat_name' and 'bot_token'.")

        self.telegram_chat_name = config["chat_name"]
        self.telegram_bot_token = config["bot_token"]

    def send_telegram_message(self, message: str, json_format: bool = False):
        """
        Отправка сообщения в Telegram.
        :param message: Сообщение для отправки.
        :return: Ответ от API Telegram.
        :raises Exception: Если запрос не был успешным.
        """
        headers = {
            'Content-Type': 'application/json',
            'Proxy-Authorization': 'Basic base64'
        }

        if json_format:
            message = json.dumps(message, indent=4, ensure_ascii=False)

        data_dict = {
            'chat_id': self.telegram_chat_name,
            'text': message,
            'parse_mode': 'HTML',
            'disable_notification': True
        }
        data = json.dumps(data_dict)
        url = f'https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage'

        response = requests.post(url, data=data, headers=headers, proxies=None, verify=False)

        if response.status_code != 200:
            raise Exception(f"Failed to send message: {response.status_code}, {response.text}")

        return response.json()
