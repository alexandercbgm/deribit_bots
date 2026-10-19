import os
import json
import traceback

from datetime import datetime, timedelta, timezone

from modules.mixins.config_mixin import ConfigMixin
from modules.mixins.telegram_mixin import TelegramMixin
from modules.mixins.deribit_mixin import DeribitMixin
from modules.mixins.historical_stat_mixin import HistoricalStatMixin
from modules.mixins.risk_management_mixin import RiskManagementMixin

class DeribitBotBase(ConfigMixin, TelegramMixin, DeribitMixin, HistoricalStatMixin, RiskManagementMixin):
    def __init__(self):
        ConfigMixin.__init__(self)
        TelegramMixin.__init__(self)
        DeribitMixin.__init__(self)
        HistoricalStatMixin.__init__(self)
        RiskManagementMixin.__init__(self)

        self.version = self.get_config_value("version")
        self.log_level = self.get_config_value("log_level")

        # Лог-файлы
        self.log_folder = self.get_config_value("paths", "log", "folder")
        os.makedirs(self.log_folder, exist_ok=True)

        self.runtime_log_template = os.path.join(
            self.log_folder,
            self.get_config_value("paths", "log", "runtime_log").replace("version", self.version)
        )

        self.trading_log_file = os.path.join(
            self.log_folder,
            self.get_config_value("paths", "log", "trading_log").replace("version", self.version)
        )

        self.trading_history = {}
        if os.path.exists(self.trading_log_file):
            with open(self.trading_log_file, "r") as fp:
                self.trading_history = json.load(fp)

        # Майнинг
        self.mining_folder = self.get_config_value("paths", "mining", "folder")
        os.makedirs(self.mining_folder, exist_ok=True)

        self.mining_book_summary_template = os.path.join(
            self.mining_folder,
            self.get_config_value("paths", "mining", "book_summary").replace("version", self.version)
        )

    def check_buy_time(self) -> bool:
        """Интерфейсный метод, который будет переопределяться в боте."""
        pass

    @property
    def trade_date(self) -> str:
        """Возвращает торговую дату, сменяющуюся в заданное время UTC."""
        now = datetime.now(timezone.utc)

        # Получаем время экспирации из конфигурации
        expire_time_today = datetime.strptime(
            self.get_config_value("times", "option_expire_time"), "%H:%M"
        ).replace(year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc)

        # Если текущее время меньше времени экспирации, относим дату к предыдущему дню
        trade_date = now.date() if now >= expire_time_today else (now - timedelta(days=1)).date()

        return trade_date.strftime("%Y-%m-%d")

    @property
    def now_d(self) -> str:
        """Возвращает текущую дату в формате строки."""
        return datetime.now().strftime("%Y-%m-%d")

    @property
    def now_dt(self) -> str:
        """Возвращает текущее время в формате строки."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def now_dt_ffn(self) -> str:
        """Возвращает текущее время в формате строки для имени файла."""
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    def start_bot(self) -> None:
        """Запуск основного цикла бота."""
        self.send_telegram_message(f"Deribit bot {self.version} started at {self.now_dt}")

        try:
            self.start_main_loop()
        except Exception as e:
            error_message = traceback.format_exc()
            self.logger.error(error_message)

            # Отправка ошибки в Telegram
            self.send_telegram_message(f"Deribit bot {self.version} failed at {self.now_dt} with error:\n{error_message}")

        self.send_telegram_message(f"Deribit bot {self.version} stopped at {self.now_dt}")
