import json
import logging
import math

import numpy as np

from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler

from modules.deribit_bot_base import DeribitBotBase

class TradingLogicError(Exception):
    pass

class DeribitBotCondor(DeribitBotBase):
    def __init__(self):
        super().__init__()

        self.version = f"{self.version}_condor"

        self.scenarios = self.get_config_value("scenarios")
        self.option_label = self.get_config_value("option_label")
        self.volatility_source = self.get_config_value("volatility_source")

        # Сценарии по монетам могут меняться
        self.scenario = {
            currency: config.get("default_direction")
            for currency, config in self.get_config_value("scenarios").items()
        }

        # Размер позиции по монетам
        self.pos_size = {
            currency: config.get("start_lot")
            for currency, config in self.get_config_value("scenarios").items()
        }

        # Настройка логирования
        log_file_path = self.runtime_log_template.replace("timestamp", self.now_dt_ffn)

        # Удаляем все существующие обработчики у корневого логгера
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        # Создаем обработчик с ротацией по времени
        handler = TimedRotatingFileHandler(
            filename=log_file_path,
            when="midnight",        # Ротация в полночь
            interval=1,             # Интервал ротации 1 день
            backupCount=7,          # Хранить последние 7 логов (опционально)
            encoding="utf-8"        # Указываем кодировку (опционально)
        )

        # Форматирование логов
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)

        # Уровень логирования
        handler.setLevel(logging.DEBUG if self.log_level == "DEBUG" else logging.INFO)

        # Настраиваем logger
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.DEBUG if self.log_level == "DEBUG" else logging.INFO)

        # Удаляем все существующие обработчики у текущего логгера (на случай дублирования)
        self.logger.handlers.clear()

        # Добавляем обработчик для файла
        self.logger.addHandler(handler)

        # Отключаем передачу событий в корневой логгер (чтобы избежать вывода в консоль)
        self.logger.propagate = False

        self.logger.debug("Инициализация завершена. Загружены сценарии: %s", self.scenarios)

    def calculate_days_to_expiry(self, option_date_eby):
        """
        Рассчитывает оставшееся время до экспирации опциона в днях.

        :return: Оставшееся время в днях (например, 0.5 для полдня).
        """
        now = datetime.now(timezone.utc)
        expire_date = datetime.strptime(option_date_eby, "%d%b%y").replace(tzinfo=timezone.utc)

        expire_datetime = datetime.strptime(
            self.get_config_value("times", "option_expire_time"), "%H:%M"
        ).replace(year=expire_date.year, month=expire_date.month, day=expire_date.day, tzinfo=timezone.utc)

        time_to_expiry_seconds = (expire_datetime - now).total_seconds()
        time_to_expiry_days = time_to_expiry_seconds / (24 * 3600)

        return time_to_expiry_days

    def currency_price_std_dev(self, volatility_perc, currency_price, expire_days):
        """
        Этот метод вычисляет стандартное отклонение цены валюты
        на основе ее исторической или предполагаемой волатильности,
        текущей цены валюты и срока до истечения опциона.

        :param volatility_perc: Волатильность в процентах.
        :param currency_price: Текущая цена валюты (spot price).
        :param expire_days: Количество дней до истечения опциона.
        :return: Стандартное отклонение цены валюты, рассчитанное на основе данных параметров.
        """
        IV = volatility_perc / 100  # Convert to decimal
        S = currency_price  # Spot price
        T = expire_days / 365  # Time to maturity in years

        result = S * IV * (T ** 0.5)

        self.logger.debug(
            "Рассчитано стандартное отклонение цены: IV=%f, S=%f, T=%f, result=%f",
            IV, S, T, result
        )

        return result

    def calculate_central_strikes(self, currency):
        """
        Этот метод вычисляет центральные страйки для опциона на основе
        текущей цены индекса валюты и заданных параметров.

        :param currency: Валюта, для которой рассчитываются центральные страйки.
        :return: Возвращает два значения: нижний и верхний страйк, на основе которых будет строиться опцион.
        """
        strike_step = self.scenarios[currency]['strike_step']
        center_strike_to_spot_proximity = self.scenarios[currency]['center_strike_to_spot_proximity']
        index_price = self.deribit_index_price[currency]

        strike_low = int(index_price / strike_step) * strike_step
        strike_high = int(index_price / strike_step) * strike_step + strike_step

        if abs(index_price - strike_low) / strike_step <= center_strike_to_spot_proximity:
            strike_high = strike_low

        if abs(index_price - strike_high) / strike_step <= center_strike_to_spot_proximity:
            strike_low = strike_high

        self.logger.debug(
            "Вычислены центральные страйки для %s: index_price=%.2f, strike_low=%d, strike_high=%d",
            currency, index_price, strike_low, strike_high
        )

        return strike_low, strike_high

    async def get_volatility(self, currency, strike_low, strike_high, date):
        """
        Этот метод получает волатильность для заданных колл- и пут-опционов для указанной валюты.

        :param currency: Валюта, для которой извлекаются данные.
        :param strike_low: Страйк для пут-опциона.
        :param strike_high: Страйк для колл-опциона.
        :param date: Дата истечения опциона, формат 'ddMMMyy' (например, '07DEC24').
        :return: Возвращает кортеж из волатильностей для колл- и пут-опционов (mark_iv_call, mark_iv_put).
        """
        if self.volatility_source == "mark_iv":
            option_name_call = f"{self.option_label[currency]}-{date}-{strike_high}-C"
            option_name_put = f"{self.option_label[currency]}-{date}-{strike_low}-P"

            # Получаем mark_iv для call и put опционов
            mark_iv_call, data_received_call = await self.get_option_attr(option_name_call, "mark_iv")
            mark_iv_put, data_received_put = await self.get_option_attr(option_name_put, "mark_iv")

            # Проверяем, получены ли данные для обоих опционов
            missing_options = [
                name for name, received in [
                    (option_name_call, data_received_call),
                    (option_name_put, data_received_put)
                ] if not received
            ]

            # Доступность инструментов проверяется в self.get_central_prices(), поэтому здесь только ждем получения волатильности
            if missing_options:
                error_msg = (
                    f"Еще не получены mark_iv по следующим страйкам для {currency}: {', '.join(missing_options)}"
                )
                self.logger.error(error_msg)
                raise TradingLogicError(error_msg)

        # elif self.volatility_source == "history":
        else:
            # Преобразуем дату из формата '%d%b%y' в '%Y-%m-%d'
            try:
                parsed_date = datetime.strptime(date, '%d%b%y') - timedelta(days=1)
            except ValueError:
                self.logger.error(f"Неверный формат даты: {date}")
                return None

            historical_volatility = self.get_historical_volatility(currency, parsed_date)

            if historical_volatility is None:
                error_msg = f"Историческая волатильность для {currency} на дату {parsed_date.strftime('%Y-%m-%d')} отсутствует."
                self.logger.error(error_msg)
                raise TradingLogicError(error_msg)

            mark_iv_call = historical_volatility
            mark_iv_put = historical_volatility

        # else:
        #     error_msg = f"Неверный источник волатильности: {self.volatility_source}."
        #     self.logger.error(error_msg)
        #     raise ValueError(error_msg)

        self.logger.debug(
            "Получена волатильность для %s: mark_iv_call=%s, mark_iv_put=%s",
            currency, mark_iv_call, mark_iv_put
        )

        return mark_iv_call, mark_iv_put

    def update_scenario(self, currency, mark_iv_call, mark_iv_put):
        """
        Этот метод обновляет сценарий торговли для заданной валюты
        в зависимости от средней волатильности колл- и пут-опционов.

        :param currency: Валюта, для которой обновляется сценарий.
        :param mark_iv_call: Волатильность для колл-опциона.
        :param mark_iv_put: Волатильность для пут-опциона.
        :return: Направление для "wing" и "center" опционов ("bid"/"ask").
        """
        fixed_direction = self.scenarios[currency].get('fixed_direction')

        if fixed_direction is not None:
            self.scenario[currency] = fixed_direction

            self.logger.debug(
                "Фиксированный сценарий для %s: scenario=%s",
                currency, self.scenario[currency]
            )

        else:
            mark_iv = (mark_iv_call + mark_iv_put) / 2
            initial_scenario = self.scenario[currency]

            if mark_iv > self.scenarios[currency]['max_short_volatility']:
                self.scenario[currency] = "long"

            if mark_iv < self.scenarios[currency]['min_long_volatility']:
                self.scenario[currency] = "short"

            self.logger.debug(
                "Обновлен сценарий для %s: mark_iv=%f, initial_scenario=%s, new_scenario=%s",
                currency, mark_iv, initial_scenario, self.scenario[currency]
            )

        wing_direction = "bid" if self.scenario[currency] == "long" else "ask"
        center_direction = "ask" if self.scenario[currency] == "long" else "bid"

        return wing_direction, center_direction

    async def get_central_prices(self, currency, strike_low, strike_high, date):
        """
        Получает цены центральных опционов колл и пут для указанных страйков.

        :param currency: Валюта, для которой запрашиваются цены (например, 'BTC', 'ETH').
        :param strike_low: Нижний страйк опциона.
        :param strike_high: Верхний страйк опциона.
        :param date: Дата экспирации опциона в формате строки (например, '7JAN25').
        :return: Кортеж, содержащий цену call-опциона и цену put-опциона.
        """
        option_name_call = f"{self.option_label[currency]}-{date}-{strike_high}-C"
        option_name_put = f"{self.option_label[currency]}-{date}-{strike_low}-P"

        center_direction = "ask" if self.scenario[currency] == "long" else "bid"

        # Доступность инструментов проверяем получением цены
        price_call, data_received_call = await self.get_option_attr(option_name_call, f"{center_direction}_price")
        price_put, data_received_put = await self.get_option_attr(option_name_put, f"{center_direction}_price")

        # Проверяем, получены ли данные для обоих опционов
        missing_options = [
            name for name, received in [
                (option_name_call, data_received_call),
                (option_name_put, data_received_put)
            ] if not received
        ]

        check_request_counts_limit = (
            currency not in self.deribit_main_coins and
            all(
                self.deribit_option_prices_alt_coins_request_counts.get(currency, {}).get(name, 0) <= self.deribit_alt_coins_request_counts_limit
                for name in [option_name_call, option_name_put]
            )
        )

        # Если превышен лимит запросов на получение данных, то считаем, что по этим инструментам None
        if missing_options and check_request_counts_limit:
            error_msg = (
                f"Еще не получены цены по следующим центральным страйкам для {currency}: {', '.join(missing_options)}"
            )
            self.logger.error(error_msg)
            raise TradingLogicError(error_msg)

        self.logger.debug(
            "Получены цены для %s: %s %s, %s %s",
            currency, option_name_call, price_call, option_name_put, price_put
        )

        return price_call, price_put

    async def get_central_strikes(self, currency, date):
        """
        Этот метод ищет центральные страйки для валюты на основе заданной даты.

        :param currency: Валюта, для которой ищутся страйки.
        :param date: Дата, на которую необходимо вычислить страйки и волатильность.
        :return: strike_low (нижний страйк), strike_high (верхний страйк),
                 mark_iv_call (волатильность для колл-опциона), mark_iv_put (волатильность для пут-опциона).
        """
        mark_iv_call = mark_iv_put = price_call = price_put = None
        strike_low_base, strike_high_base = self.calculate_central_strikes(currency)
        strike_multiplier = 0

        while not price_call or not price_put:
            strike_low = strike_low_base - self.scenarios[currency]['strike_step'] * strike_multiplier
            strike_high = strike_high_base + self.scenarios[currency]['strike_step'] * strike_multiplier

            # Проверяем доступность инструментов на этих страйках
            price_call, price_put = await self.get_central_prices(currency, strike_low, strike_high, date)
            mark_iv_call, mark_iv_put = await self.get_volatility(currency, strike_low, strike_high, date)

            self.logger.debug(
                "strike_multiplier %d: strike_low=%d, strike_high=%d, mark_iv_call=%s, mark_iv_put=%s, price_call=%s, price_put=%s",
                strike_multiplier, strike_low, strike_high, mark_iv_call, mark_iv_put, price_call, price_put
            )

            strike_multiplier += 1

            if strike_multiplier > self.scenarios[currency]['max_center_strike_multiplier']:
                error_msg = (
                    f"Не удалось найти центральные страйки: {currency} "
                    f"{self.deribit_index_price[currency]} {strike_low} {strike_high}"
                )
                self.logger.error(error_msg)
                raise TradingLogicError(error_msg)

        return strike_low, strike_high, mark_iv_call, mark_iv_put

    async def get_condor_options(self, currency, strike_low, strike_high, date, center_dir, wing_dir, center_offset, wing_offset):
        """
        Получает опционы для стратегии кондора.

        :param currency: Валюта.
        :param strike_low: Нижний страйк.
        :param strike_high: Верхний страйк.
        :param date: Дата истечения.
        :param center_dir: Направление для центра.
        :param wing_dir: Направление для крыльев.
        :param center_offset: Смещение для центра.
        :param wing_offset: Смещение для крыльев.
        :return: Список имен опционов и их цен.
        """
        # Опционы кондора
        call_wing_name = f"{self.option_label[currency]}-{date}-{strike_high + wing_offset}-C"
        call_center_name = f"{self.option_label[currency]}-{date}-{strike_high + center_offset}-C"
        put_center_name = f"{self.option_label[currency]}-{date}-{strike_low - center_offset}-P"
        put_wing_name = f"{self.option_label[currency]}-{date}-{strike_low - wing_offset}-P"

        # Цены кондора
        call_wing_price, call_wing_data_received = await self.get_option_attr(call_wing_name, f"{wing_dir}_price")
        call_center_price, call_center_data_received = await self.get_option_attr(call_center_name, f"{center_dir}_price")
        put_center_price, put_center_data_received = await self.get_option_attr(put_center_name, f"{center_dir}_price")
        put_wing_price, put_wing_data_received = await self.get_option_attr(put_wing_name, f"{wing_dir}_price")

        # Проверяем, получены ли данные для всех опционов
        missing_options = [
            name for name, received in [
                (call_wing_name, call_wing_data_received),
                (call_center_name, call_center_data_received),
                (put_center_name, put_center_data_received),
                (put_wing_name, put_wing_data_received)
            ] if not received
        ]

        check_request_counts_limit = (
            currency not in self.deribit_main_coins and
            all(
                self.deribit_option_prices_alt_coins_request_counts.get(currency, {}).get(name, 0) <= self.deribit_alt_coins_request_counts_limit
                for name in [call_wing_name, call_center_name, put_center_name, put_wing_name]
            )
        )

        # Если превышен лимит запросов на получение данных, то считаем, что по этим инструментам None
        if missing_options and check_request_counts_limit:
            error_msg = (
                f"Еще не получены цены опционов по следующим страйкам для {currency}: {', '.join(missing_options)}"
            )
            self.logger.error(error_msg)
            raise TradingLogicError(error_msg)

        option_names = [call_wing_name, call_center_name, put_center_name, put_wing_name]
        option_prices = [call_wing_price, call_center_price, put_center_price, put_wing_price]

        if all(option_prices):
            self.logger.debug(
                "Получены цены опционов для %s: names=%s, prices=%s",
                currency, option_names, option_prices
            )
        else:
            self.logger.warning(
                "Не удалось получить все цены опционов для %s: names=%s, prices=%s",
                currency, option_names, option_prices
            )

        return option_names, option_prices

    def get_condor_cost(self, currency, option_prices):
        direction_mul = 1 if self.scenario[currency] == "long" else -1

        cost = direction_mul * (-option_prices[0] + option_prices[1] + option_prices[2] - option_prices[3])

        # Если цена опциона уже идет в USD
        if self.option_label[currency].find("USD") >= 0:
            cost_usd = cost
        else:
            cost_usd = cost * self.deribit_index_price[currency]

        return cost, cost_usd

    def get_condor_profit(self, currency, option_prices, strike_low, strike_high, center_offset, wing_offset):
        """
        Вычисляет потенциальную прибыль для стратегии кондора.

        :param currency: Валюта.
        :param option_prices: Цены на опционы.
        :param strike_low: Нижний страйк.
        :param strike_high: Верхний страйк.
        :param center_offset: Смещение для центра.
        :param wing_offset: Смещение страйков крыльев.
        :return: Потенциальная прибыль и риск.
        """
        # Переводим стоимость кондора из монеты в USD
        _, cost_usd = self.get_condor_cost(currency, option_prices)

        if self.scenario[currency] == "long":
            profit = (wing_offset - center_offset) - cost_usd
            risk = cost_usd
        elif self.scenario[currency] == "short":
            profit = abs(cost_usd)
            risk = (wing_offset - center_offset) - abs(cost_usd)
        else:
            self.logger.error("Неизвестный сценарий: %s для %s", self.scenario[currency], currency)
            raise ValueError(f"Invalid scenario: {self.scenario[currency]}")

        if profit <= 0.0:
            self.logger.warning(
                "Прибыль нулевая для %s: cost_usd=%f, profit=%f, center_offset=%f, wing_offset=%f",
                currency, cost_usd, profit, center_offset, wing_offset
            )
        else:
            self.logger.debug(
                "Рассчитана прибыль для %s: cost_usd=%f, profit=%f, center_offset=%f, wing_offset=%f",
                currency, cost_usd, profit, center_offset, wing_offset
            )

        return profit, risk

    async def get_optimal_condor(self, currency, strike_low, strike_high, mark_iv_call, mark_iv_put, date, price_move):
        """
        Поиск оптимального кондора для заданной валюты.

        :param currency: Валюта.
        :param strike_low: Нижний страйк.
        :param strike_high: Верхний страйк.
        :param mark_iv_call: Имплайд волатильность коллов.
        :param mark_iv_put: Имплайд волатильность путов.
        :param date: Дата истечения опционов.
        :param price_move: Ожидаемое изменение цены.
        :return: Кортеж (лучшие имена опционов, лучшие цены, максимальная прибыль, ее риск).
        """
        wing_size = self.get_config_value("scenarios", currency, "wing_size")
        min_wing_bonus_perc = self.get_config_value("scenarios", currency, "min_wing_bonus_perc")

        wing_dir, center_dir = self.update_scenario(currency, mark_iv_call, mark_iv_put)

        # Получаем стоимость крыла на деньгах для оценки соотношения профита и лосса в случае раллирования, для откупа меняем местами center_dir и wing_dir
        self.logger.debug(f"Получение цен закрытия крыла на деньгах, будут получены цены: {wing_dir} и {center_dir}")

        option_otm_names, option_otm_prices = await self.get_condor_options(currency, strike_low, strike_high, date, wing_dir, center_dir, 0, wing_size)
        if not all(option_otm_prices):
            error_msg = f"Некоторые цены кондора на деньгах имеют значение None: {option_otm_names} {option_otm_prices}"
            self.logger.error(error_msg)
            raise TradingLogicError(error_msg)
        condor_otm_cost, condor_otm_cost_usd = self.get_condor_cost(currency, option_otm_prices)

        ineffective_move_high = abs(strike_high - self.deribit_index_price[currency])
        ineffective_move_low = abs(self.deribit_index_price[currency] - strike_low)
        ineffective_move = max(ineffective_move_high, ineffective_move_low)

        # В большую сторону не округляем, чтобы не терять прибыль, в меньшую - чтобы лишний раз не раллировать
        center_offset = round(
            (price_move - ineffective_move) / self.scenarios[currency]['strike_step']
        ) * self.scenarios[currency]['strike_step']
        wing_offset = center_offset + wing_size

        option_names, option_prices = await self.get_condor_options(currency, strike_low, strike_high, date, center_dir, wing_dir, center_offset, wing_offset)
        if not all(option_prices):
            error_msg = f"Некоторые цены целевого кондора имеют значение None: {option_names} {option_prices}"
            self.logger.error(error_msg)
            raise TradingLogicError(error_msg)
        condor_cost, condor_cost_usd = self.get_condor_cost(currency, option_prices)

        profit, risk = self.get_condor_profit(currency, option_prices, strike_low, strike_high, center_offset, wing_offset)

        if profit <= 0.0:
            error_msg = f"Профит отрицательный для {currency}: {profit}"
            self.logger.error(error_msg)
            raise TradingLogicError(error_msg)

        wing_bonus_perc = condor_cost_usd / condor_otm_cost_usd
        if wing_bonus_perc < min_wing_bonus_perc:
            error_msg = f"Недостаточная доля профита крыла кондора: {wing_bonus_perc:.2f} < {min_wing_bonus_perc:.2f}"
            self.logger.error(error_msg)
            raise TradingLogicError(error_msg)

        self.logger.debug(f"Стоимость крыла кондора и крыла на деньгах: {condor_cost_usd / 2:.2f} и {condor_otm_cost_usd / 2:.2f}, perc {wing_bonus_perc:.2f}")

        return option_names, option_prices, profit, risk, wing_bonus_perc

    def update_pos_size(self, currency, buy_date_prev_ymd, probable_profit):
        """
        Обновляет размер позиции на основе истории торговли и изменений капитала.

        :param currency: Валюта.
        :param buy_date_prev_ymd: Дата предыдущей сделки (YYYY-MM-DD).
        :param probable_profit: Ожидаемая прибыль.
        :return: Кортеж (amount, equity_prev, equity, equity_delta, equity_delta_cumm).
        """
        equity_prev = equity_delta_cumm_prev = 0.0
        if currency in self.trading_history and buy_date_prev_ymd in self.trading_history[currency]:
            equity_prev = self.trading_history[currency][buy_date_prev_ymd]['account']['equity']
            equity_delta_cumm_prev = self.trading_history[currency][buy_date_prev_ymd]['account']['equity_delta_cumm']

        equity = equity_delta = equity_delta_cumm = 0.0
        for info_dict in self.deribit_account_info['summaries']:
            if info_dict['currency'] == currency:
                equity = info_dict['equity']
                equity_delta = equity - equity_prev if equity_prev > 0.0 else 0.0
                equity_delta_cumm = equity_delta_cumm_prev + equity_delta
                break

        if equity_delta < 0.0 and not self.scenarios[currency]['fixed_lot']:
            compensative_lot = equity_delta / (probable_profit * self.scenarios[currency]['contract_size'])
            amount = self.pos_size[currency] + self.scenarios[currency]['default_lot'] + compensative_lot
        else:
            amount = self.scenarios[currency]['default_lot']

        # На deribit введено ограничение - теперь лоты могут быть только целыми, поэтому округляем в большую сторону
        amount_ceil = math.ceil(amount)

        status = "Фиксированный" if self.scenarios[currency]['fixed_lot'] else "Обновлен"
        self.logger.debug(
            "%s размер позиции для %s: amount=%f, amount_ceil=%d, equity_prev=%f, equity=%f, equity_delta=%f, equity_delta_cumm=%f",
            status, currency, amount, amount_ceil, equity_prev, equity, equity_delta, equity_delta_cumm
        )

        self.pos_size[currency] = amount_ceil

        return amount_ceil, equity_prev, equity, equity_delta, equity_delta_cumm

    def check_risk_management(self, currency, buy_date_prev_ymd):
        """
        Проверка риск-менеджмента.

        :param currency: Валюта для анализа.
        :param probable_profit: Вероятный профит.
        :param risk: Риск.
        :param amount: Количество лотов.
        :return: True, если проходит проверки, иначе False.
        """
        rolling_limit = self.get_config_value("risk_management", currency, "rolling_limit")
        rolling_count = self.trading_history.get(currency, {}).get(buy_date_prev_ymd, {}).get('rolling_count', 0)

        # Проверка ограничения на количество раллирований
        if rolling_count > rolling_limit:
            self.logger.warning("Превышено количество раллирований: rolling_count=%d > rolling_limit=%d", rolling_count, rolling_limit)
            return False

        return True

    async def trade_condor(self, currency, option_names, option_prices, pos_size):
        """
        Осуществляет сделки по опционам для кондора.

        :param currency: Валюта для торговли.
        :param option_names: Имена инструментов для покупки/продажи.
        :param option_prices: Цены на сделки.
        :param pos_size: Размер позиции.
        """
        self.logger.info("Начало торговли опционами для %s: option_names=%s, pos_size=%f", currency, option_names, pos_size)

        # Сначала покупаем, потом продаем, чтобы не иметь не покрытых коротких позиций в случае накладок
        if self.scenario[currency] == "long":
            await self.buy_option(instrument_name=option_names[1], amount=pos_size, price=option_prices[1], order_type='market_limit')
            await self.buy_option(instrument_name=option_names[2], amount=pos_size, price=option_prices[2], order_type='market_limit')
            await self.sell_option(instrument_name=option_names[0], amount=pos_size, price=option_prices[0], order_type='market_limit')
            await self.sell_option(instrument_name=option_names[3], amount=pos_size, price=option_prices[3], order_type='market_limit')
        elif self.scenario[currency] == "short":
            await self.buy_option(instrument_name=option_names[0], amount=pos_size, price=option_prices[0], order_type='market_limit')
            await self.buy_option(instrument_name=option_names[3], amount=pos_size, price=option_prices[3], order_type='market_limit')
            await self.sell_option(instrument_name=option_names[1], amount=pos_size, price=option_prices[1], order_type='market_limit')
            await self.sell_option(instrument_name=option_names[2], amount=pos_size, price=option_prices[2], order_type='market_limit')
        else:
            self.logger.error("Неизвестный сценарий: %s для %s", self.scenario[currency], currency)
            raise ValueError(f"Invalid scenario: {self.scenario[currency]}")

    async def trade_condor_wing(self, currency, option_names, option_prices, pos_size, operation_type):
        """
        Осуществляет сделки с крылом кондора.

        :param currency: Валюта для торговли.
        :param option_names: Имена инструментов для покупки/продажи.
        :param option_prices: Цены на сделки.
        :param pos_size: Размер позиции.
        :param operation_type: Тип операции.
        """
        if operation_type == "open":
            self.logger.info("Покупаем крыло кондора для %s: option_names=%s, pos_size=%f", currency, option_names, pos_size)

            if self.scenario[currency] == "long":
                await self.buy_option(instrument_name=option_names[0], amount=pos_size, price=option_prices[0], order_type='market_limit')
                await self.sell_option(instrument_name=option_names[1], amount=pos_size, price=option_prices[1], order_type='market_limit')
            elif self.scenario[currency] == "short":
                await self.buy_option(instrument_name=option_names[1], amount=pos_size, price=option_prices[1], order_type='market_limit')
                await self.sell_option(instrument_name=option_names[0], amount=pos_size, price=option_prices[0], order_type='market_limit')
            else:
                self.logger.error("Неизвестный сценарий: %s для %s", self.scenario[currency], currency)
                raise ValueError(f"Invalid scenario: {self.scenario[currency]}")

        elif operation_type == "close":
            self.logger.info("Продаем крыло кондора для %s: option_names=%s, pos_size=%f", currency, option_names, pos_size)

            if self.scenario[currency] == "long":
                await self.buy_option(instrument_name=option_names[1], amount=pos_size, price=option_prices[1], order_type='market_limit')
                await self.sell_option(instrument_name=option_names[0], amount=pos_size, price=option_prices[0], order_type='market_limit')
            elif self.scenario[currency] == "short":
                await self.buy_option(instrument_name=option_names[0], amount=pos_size, price=option_prices[0], order_type='market_limit')
                await self.sell_option(instrument_name=option_names[1], amount=pos_size, price=option_prices[1], order_type='market_limit')
            else:
                self.logger.error("Неизвестный сценарий: %s для %s", self.scenario[currency], currency)
                raise ValueError(f"Invalid scenario: {self.scenario[currency]}")

        else:
            self.logger.error("Неизвестный тип операции: %s для %s", operation_type, currency)
            raise ValueError(f"Invalid type operation: {operation_type}")

    def trading_history_processing(
            self, currency, option_names, option_prices, amount,
            equity_prev, equity, equity_delta, equity_delta_cumm,
            mark_iv, price_move,
            probable_profit, risk, wing_bonus_perc, check_rm
        ):
        """
        Обработка и запись истории сделок.

        :param currency: Валюта сделки.
        :param option_names: Имена опционов.
        :param option_prices: Цены опционов.
        :param amount: Количество лотов.
        :param equity_prev: Предыдущая величина капитала.
        :param equity: Текущая величина капитала.
        :param equity_delta: Изменение капитала.
        :param equity_delta_cumm: Кумулятивное изменение капитала.
        :param mark_iv: Волатильность.
        :param price_move: Движение цены.
        :param probable_profit: Вероятный профит.
        :param risk: Риск.
        :param wing_bonus_perc: Доля премии нового крыла при раллировании.
        :param check_rm: Результат проверки риск-менеджмента.
        """
        if currency not in self.trading_history:
            self.trading_history[currency] = {}

        if self.trade_date not in self.trading_history[currency]:
            self.trading_history[currency][self.trade_date] = {}

        cost, cost_usd = self.get_condor_cost(currency, option_prices)

        self.trading_history[currency][self.trade_date].update({
            "currency": f"{currency} {self.scenarios[currency]['contract_size']}",
            "scenario": self.scenario[currency],
            "account": {
                "equity_prev": equity_prev,
                "equity": equity,
                "equity_delta": equity_delta,
                "equity_delta_cumm": equity_delta_cumm,
            },
            "index_price": self.deribit_index_price[currency],
            "mark_iv": round(mark_iv, 2),
            "price_move": round(price_move, 2),
            "options": {
                "call_wing_name": (option_names[0], option_prices[0]),
                "call_center_name": (option_names[1], option_prices[1]),
                "put_center_name": (option_names[2], option_prices[2]),
                "put_wing_name": (option_names[3], option_prices[3]),
                "wing_bonus_perc": round(wing_bonus_perc, 2),
                "cost": round(cost, 4),
                "cost_usd": round(cost_usd, 2),
                "probable_profit": round(probable_profit, 2),
                "risk": round(risk, 2),
            },
            "amount": amount,
            "amount_cost": round(cost * amount, 4),
            "amount_cost_usd": round(cost_usd * amount, 2),
            "risk_management": {
                "rolling_count": 0,
                "check_risk_management": check_rm,
            },
        })

        self.send_telegram_message(self.trading_history[currency][self.trade_date], json_format=True)

        # Запись в лог-файл
        try:
            with open(self.trading_log_file, 'w') as fp:
                json.dump(self.trading_history, fp, indent=4, ensure_ascii=False)

            self.logger.info("Торговая история успешно обновлена для %s %s.", currency, self.trade_date)
            self.logger.debug("В торговую историю добавлены данные %s %s: %s", currency, self.trade_date, self.trading_history[currency][self.trade_date])
        except Exception as e:
            self.logger.error("Ошибка при записи истории торгов: %s", str(e))

    # Переопределяем интерфейсный метод DeribitBotBase (шапка должна точно совпадать)
    def check_buy_time(self):
        """
        Проверяет, входит ли текущее время в диапазон разрешенного времени для покупки/продажи.
        """
        return True # TEST

        # 08:05-08:30 UTC - время покупки / продажи кондора, на 5 минут сдвигаем время начала покупки,
        # чтобы избежать ошибки settlement_in_progress и дождаться новой часовой свечи 08:00 UTC от yfinance
        open_pos_start_time_str = self.get_config_value("times", "condor", "open_pos_start_time")
        open_pos_end_time_str = self.get_config_value("times", "condor", "open_pos_end_time")

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

    def replace_strike(self, option_str, strike):
        parts = option_str.split("-")
        parts[2] = str(strike)

        return "-".join(parts)

    # Переопределяем интерфейсный метод DeribitMixin (шапка должна точно совпадать)
    async def trading_logic(self, currency) -> None:
        """
        Логика торговли для заданной валюты.

        :param currency: Валюта.
        """
        try:
            option_date_eby = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%e%b%y").strip().upper()
            buy_date_prev_ymd = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

            self.logger.debug(
                "Начало логики торговли для %s: option_date_eby=%s, buy_date_prev_ymd=%s",
                currency, option_date_eby, buy_date_prev_ymd
            )

            # Проверка, нужно ли открывать позицию
            if self.check_buy_time() and not self.trading_history.get(currency, {}).get(self.trade_date):
                self.logger.debug("Открытие новой позиции для %s.", currency)

                # Получаем центральные страйки и волатильность
                strike_low, strike_high, mark_iv_call, mark_iv_put = await self.get_central_strikes(currency, option_date_eby)
                self.logger.debug(
                    "Центральные страйки: strike_low=%d, strike_high=%d, mark_iv_call=%s, mark_iv_put=%s",
                    strike_low, strike_high, mark_iv_call, mark_iv_put
                )

                # Рассчитываем вероятное отклонение цены
                if self.volatility_source == "ewm":
                    price_move = self.get_absolute_body_ewm(
                        currency,
                        datetime.strptime(option_date_eby, '%d%b%y') - timedelta(days=1)
                    )
                else:
                    price_move = self.currency_price_std_dev(
                        (mark_iv_call + mark_iv_put) / 2,
                        self.deribit_index_price[currency],
                        self.calculate_days_to_expiry(option_date_eby)
                    )

                self.logger.debug("Рассчитано вероятное отклонение цены по %s: price_move=%f", self.volatility_source, price_move)

                # Получаем параметры для кондора
                option_names, option_prices, probable_profit, risk, wing_bonus_perc = await self.get_optimal_condor(
                    currency, strike_low, strike_high, mark_iv_call, mark_iv_put, option_date_eby, price_move
                )
                self.logger.debug(
                    "Параметры кондора: option_names=%s, option_prices=%s, probable_profit=%f, risk=%f",
                    option_names, option_prices, probable_profit, risk
                )

                # Обновляем размер позиции
                amount, equity_prev, equity, equity_delta, equity_delta_cumm = self.update_pos_size(
                    currency, buy_date_prev_ymd, probable_profit
                )

                # Проверяем риск-менеджмент
                check_rm = self.check_risk_management(currency, buy_date_prev_ymd)
                self.logger.debug("Проверка риск-менеджмента: check_rm=%s", check_rm)

                if check_rm:
                    # Выполняем сделки
                    await self.trade_condor(currency, option_names, option_prices, amount)
                    self.logger.debug("Сделки по опционам выполнены для %s.", currency)

                # Обрабатываем историю сделок
                self.trading_history_processing(
                    currency, option_names, option_prices, amount,
                    equity_prev, equity, equity_delta, equity_delta_cumm,
                    (mark_iv_call + mark_iv_put) / 2, price_move,
                    probable_profit, risk, wing_bonus_perc, check_rm
                )
                self.logger.debug("История сделок обновлена для %s.", currency)

                # Очищаем данные цен альт коинов, чтобы они обновились перед следующей сделкой
                self.clear_option_prices(currency)

            # Если есть позиция, то проверяем, нужно ли раллировать
            elif self.trading_history.get(currency, {}).get(self.trade_date):
                check_rm_rolling = self.check_risk_management(currency, buy_date_prev_ymd)
                self.logger.debug("Проверка риск-менеджмента при раллировании для %s: check_rm_rolling=%s", currency, check_rm_rolling)

                if not check_rm_rolling:
                    raise TradingLogicError("TradingLogicError: см. причину выше.")

                self.logger.debug("Проверка необходимости раллирования для %s.", currency)

                wing_direction = "bid" if self.scenario[currency] == "long" else "ask"
                center_direction = "ask" if self.scenario[currency] == "long" else "bid"

                # Опционы основной позиции
                call_wing_name = self.trading_history[currency][self.trade_date]["options"]["call_wing_name"][0]
                call_center_name = self.trading_history[currency][self.trade_date]["options"]["call_center_name"][0]
                put_center_name = self.trading_history[currency][self.trade_date]["options"]["put_center_name"][0]
                put_wing_name = self.trading_history[currency][self.trade_date]["options"]["put_wing_name"][0]

                # Количество закрываемых при раллировании лотов
                amount_close = self.trading_history[currency][self.trade_date]["amount"]

                option_names_rolling = None
                amount_rolling = None

                # Если есть раллирование, то берем актуальное крыло
                rolling_count = self.trading_history[currency][self.trade_date]["risk_management"]["rolling_count"]
                only_close_wing = False

                if rolling_count > 0:
                    only_close_wing = self.trading_history[currency][self.trade_date]["rolling"][rolling_count]["only_close_wing"]

                    if only_close_wing:
                        self.logger.debug("При проверке раллирования для %s: only_close_wing=%s", currency, only_close_wing)
                        return None

                    option_names_rolling = self.trading_history[currency][self.trade_date]["rolling"][rolling_count]["open"]["option_names_rolling"]
                    amount_rolling = self.trading_history[currency][self.trade_date]["rolling"][rolling_count]["open"]["amount_rolling"]

                    # Если крыло с колами
                    if option_names_rolling[0].split('-')[-1] == 'C':
                        call_center_name = option_names_rolling[0]
                        call_wing_name = option_names_rolling[1]
                    # Если крыло с путами
                    else:
                        put_center_name = option_names_rolling[0]
                        put_wing_name = option_names_rolling[1]

                option_names = None
                move_dir = 0

                # 0 - центральный опцион, 1 - крыло
                if self.deribit_index_price[currency] >= float(call_center_name.split('-')[-2]):
                    option_names = [call_center_name, call_wing_name]
                    move_dir = 1
                elif self.deribit_index_price[currency] <= float(put_center_name.split('-')[-2]):
                    option_names = [put_center_name, put_wing_name]
                    move_dir = -1
                else:
                    self.logger.debug("Нет необходимости раллирования для %s.", currency)
                    return None

                # Если закрываемое крыло уже раллировалось, то берем актуальное количество лотов
                if option_names_rolling and amount_rolling and option_names == option_names_rolling:
                    amount_close = amount_rolling

                # Если нужно раллировать
                if option_names and not only_close_wing:
                    self.logger.debug("Нужно раллировать для %s: %s", currency, option_names)

                    # Раз мы закрываем позицию, то bid и ask нужно поменять местами
                    option_price_0, data_received_0 = await self.get_option_attr(option_names[0], f"{wing_direction}_price")
                    option_price_1, data_received_1 = await self.get_option_attr(option_names[1], f"{center_direction}_price")

                    # Проверяем, получены ли данные для обоих опционов
                    missing_options = [
                        name for name, received in [
                            (option_names[0], data_received_0),
                            (option_names[1], data_received_1)
                        ] if not received
                    ]

                    if missing_options:
                        error_msg = (
                            f"Еще не получены цены по следующим центральным страйкам для {currency}: {', '.join(missing_options)}"
                        )
                        self.logger.error(error_msg)
                        raise TradingLogicError(error_msg)

                    center_strike = int(option_names[0].split('-')[-2])
                    wing_strike = int(option_names[1].split('-')[-2])

                    strike_step = self.scenarios[currency]["strike_step"]
                    index_price_open_pos = self.trading_history[currency][self.trade_date]["index_price"]

                    rolling_offset = int(abs(index_price_open_pos - center_strike) / strike_step) * strike_step

                    # Получаем ближайшие к текущей цене страйки
                    strike_low_rolling, strike_high_rolling, _, __ = await self.get_central_strikes(currency, option_date_eby)
                    self.logger.debug("Центральные страйки для %s: strike_low_rolling=%d, strike_high_rolling=%d", currency, strike_low_rolling, strike_high_rolling)

                    center_strike_rolling = strike_high_rolling if move_dir == 1 else strike_low_rolling

                    strike_multiplier = 0
                    option_price_r0 = option_price_r1 = None

                    while not option_price_r0 or not option_price_r1:
                        center_strike_rolling =  center_strike_rolling + move_dir * (rolling_offset + strike_step * strike_multiplier)
                        wing_strike_rolling = center_strike_rolling + abs(center_strike - wing_strike) + move_dir * (rolling_offset + strike_step * strike_multiplier)

                        option_names_rolling = [
                            self.replace_strike(option_names[0], center_strike_rolling),
                            self.replace_strike(option_names[1], wing_strike_rolling)
                        ]

                        option_price_r0, data_received_r0 = await self.get_option_attr(option_names_rolling[0], f"{center_direction}_price")
                        option_price_r1, data_received_r1 = await self.get_option_attr(option_names_rolling[1], f"{wing_direction}_price")

                        option_prices_rolling = [option_price_r0, option_price_r1]

                        if not option_price_r0 or not option_price_r1:
                            self.logger.warning(
                                "Не удалось найти все цены при раллировании для %s: option_names_rolling=%s, option_prices_rolling=%s",
                                currency, option_names_rolling, [option_price_r0, option_price_r1]
                            )

                        strike_multiplier += 1

                        if strike_multiplier > self.scenarios[currency]['max_center_strike_multiplier']:
                            only_close_wing = True
                            break

                    # Закрываем позиции по старому крылу
                    option_prices = [option_price_0, option_price_1]

                    cost = (option_prices[0] - option_prices[1]) * (1 if self.scenario[currency] == "long" else -1)
                    cost_usd = cost * self.deribit_index_price[currency]

                    self.logger.debug(
                        "Закрываем крыло для %s: option_names=%s, option_prices=%s, amount_close=%f",
                        currency, option_names, option_prices, amount_close
                    )

                    # self.trade_condor_wing(
                    #     currency,
                    #     option_names,
                    #     option_prices,
                    #     amount_close,
                    #     "close"
                    # )

                    if not only_close_wing:
                        cost_close_wing = self.trading_history[currency][self.trade_date]["options"]["cost"] / 2

                        cost_rolling = (option_prices_rolling[0] - option_prices_rolling[1]) * (1 if self.scenario[currency] == "long" else -1)
                        cost_rolling_usd = cost_rolling * self.deribit_index_price[currency]

                        rolling_lot_limit = self.get_config_value("risk_management", currency, "rolling_lot_limit")

                        # Предполагаем, что соотношение стоимости крыльев вне денег и на деньгах не ухудшилось для нас
                        amount_rolling = amount_close * (cost - cost_rolling - cost_close_wing) / cost_rolling
                        amount_rolling = amount_rolling if amount_rolling < rolling_lot_limit else rolling_lot_limit

                        # На deribit введено ограничение - теперь лоты могут быть только целыми, поэтому округляем в большую сторону
                        amount_rolling_ceil = math.ceil(amount_rolling)

                        self.logger.debug(
                            "Расчитан amount_rolling для %s: amount_close=%.2f, cost=%.4f, cost_rolling=%.4f, cost_close_wing=%.4f, amount_rolling=%.2f, amount_rolling_ceil=%d",
                            currency, amount_close, cost, cost_rolling, cost_close_wing, amount_rolling, amount_rolling_ceil
                        )

                        self.logger.debug(
                            "Открываем новое крыло для %s: option_names_rolling=%s, option_prices_rolling=%s, amount_rolling=%.2f, amount_rolling_ceil=%d",
                            currency, option_names_rolling, option_prices_rolling, amount_rolling, amount_rolling_ceil
                        )

                        # Открываем позиции по новому крылу
                        # self.trade_condor_wing(
                        #     currency,
                        #     option_names_rolling,
                        #     option_prices_rolling,
                        #     amount_rolling_ceil,
                        #     "open"
                        # )

                        # Увеличиваем счетчик раллирования на 1
                        self.trading_history[currency][self.trade_date]["risk_management"]["rolling_count"] += 1
                    else:
                        cost_rolling = 0.0
                        cost_rolling_usd = 0.0
                        amount_rolling_ceil = 0

                    rolling_dict = {
                        self.trading_history[currency][self.trade_date]["risk_management"]["rolling_count"]: {
                            "close": {
                                "option_names": option_names,
                                "option_prices": option_prices,
                                "cost": round(cost, 4),
                                "cost_usd": round(cost_usd, 2),
                            },
                            "open": {
                                "option_names_rolling": option_names_rolling,
                                "option_prices_rolling": option_prices_rolling,
                                "cost_rolling": round(cost_rolling, 4),
                                "cost_rolling_usd": round(cost_rolling_usd, 2),
                                "amount_rolling": amount_rolling_ceil
                            },
                            "only_close_wing": only_close_wing,
                        }
                    }

                    self.trading_history[currency][self.trade_date]["rolling"] = rolling_dict

                    self.send_telegram_message(self.trading_history[currency][self.trade_date], json_format=True)

                    # Запись в лог-файл
                    try:
                        with open(self.trading_log_file, 'w') as fp:
                            json.dump(self.trading_history, fp, indent=4, ensure_ascii=False)

                        self.logger.info("Торговая история успешно обновлена для %s %s.", currency, self.trade_date)
                        self.logger.debug("В торговую историю добавлены данные %s %s: %s", currency, self.trade_date, self.trading_history[currency][self.trade_date])
                    except Exception as e:
                        self.logger.error("Ошибка при записи истории торгов: %s", str(e))

            else:
                self.logger.debug("Позиция для %s не открывается: либо не время, либо уже есть сделка.", currency)

        except TradingLogicError as e:
            self.logger.error("Ошибка в логике торговли для %s: %s", currency, str(e))
