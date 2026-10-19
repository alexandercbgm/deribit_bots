import os
import json

from datetime import datetime, timezone

from modules.deribit_bot_base import DeribitBotBase

class DeribitBotHistoryMiner(DeribitBotBase):
    def __init__(self):
        super().__init__()

        self.version = f"{self.version}_history_miner"

    # Переопределяем интерфейсный метод DeribitBotBase (шапка должна точно совпадать)
    def check_buy_time(self):
        """
        Проверяет, входит ли текущее время в диапазон разрешенного времени для майнинга исторических данных.
        """
        # Для тестов
        # return True

        # Время майнинга исторических данных (08:05 - 08:15 UTC)
        open_pos_start_time_str = self.get_config_value("times", "history_miner", "open_pos_start_time")
        open_pos_end_time_str = self.get_config_value("times", "history_miner", "open_pos_end_time")

        # Преобразуем строки в объекты datetime
        now = datetime.now(timezone.utc)
        open_pos_start_time = datetime.strptime(open_pos_start_time_str, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc
        )
        open_pos_end_time = datetime.strptime(open_pos_end_time_str, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc
        )

        # Возвращает True, если текущее время в диапазоне
        return open_pos_start_time <= now <= open_pos_end_time

    # Переопределяем интерфейсный метод DeribitMixin (шапка должна точно совпадать)
    async def trading_logic(self, currency) -> None:
        """
        Логика сохранения истории для заданной валюты.

        :param currency: Валюта.
        """
        mining_file = self.mining_book_summary_template \
                .replace("currency", f"{currency}".lower()) \
                .replace("date", self.now_d)

        if self.check_buy_time() and not os.path.exists(mining_file):
            # Записываем дневные исторические данные один раз в начале дня
            with open(mining_file, 'w') as fp:
                if currency in self.deribit_main_coins:
                    json.dump(self.deribit_option_prices_main_coins[currency], fp)
                else:
                    # по альт коинам выгружаем пустой словарь - по ним нет данных в get_book_summary_by_currency
                    # json.dump(self.deribit_option_prices_alt_coins[currency], fp)
                    json.dump(self.deribit_option_prices_alt_coins, fp)

            self.send_telegram_message(f"Deribit bot {self.version} write {currency} file at {self.now_dt}")
