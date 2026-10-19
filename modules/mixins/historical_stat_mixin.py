import os
import yaml

import pandas as pd
import numpy as np
import yfinance as yf

from datetime import datetime, timedelta, timezone


class HistoricalStatMixin:
    def __init__(self):
        # Загрузка параметров из конфигурационного файла
        config_path = "config/historical_stat_config.yaml"
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found at {config_path}")

        with open(config_path, "r") as file:
            self.config = yaml.safe_load(file)

        # Инициализация кеша для данных
        self.data_cache = {}
        self.data_08_cache = {}

    def get_historical_stat(self, currency: str) -> pd.DataFrame:
        """
        Загружает исторические данные с помощью библиотеки yfinance.
        Заменяет `CURRENCY` в тикере на указанную валюту.
        """
        if currency in self.data_cache:
            last_hour = self.data_cache[currency]['Datetime'].max().replace(tzinfo=timezone.utc)
            now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

            if last_hour >= now_hour:
                return self.data_cache[currency]
            else:
                # Очищаем кеш 08 для currency, чтобы пересчитать его в get_data_08()
                if currency in self.data_08_cache:
                    del self.data_08_cache[currency]

        ticker = self.config["ticker"].replace("CURRENCY", currency)
        period = self.config["period"]
        interval = self.config["interval"]

        data = (
            yf.download(
                tickers=ticker,
                period=period,
                interval=interval
            )
            .reset_index()
        )

        # Убираем верхний уровень индекса
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        data = data[['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']]

        self.data_cache[currency] = data
        return data

    def get_data_08(self, currency: str, date: datetime = None) -> pd.DataFrame:
        """
        Пересчитывает часовые свечи в дневные с интервалом с 8:00 до 8:00.
        Возвращает данные за год назад от указанной даты (исключая эту дату).
        Если данные уже есть в кеше и они актуальны, повторная загрузка не выполняется.
        """
        expiration_hour = 8
        # expiration_hour = 4 # TEST

        # Проверка кеша
        if currency in self.data_08_cache:
            return self.data_08_cache[currency]

        # Загрузка данных и обработка
        data = self.get_historical_stat(currency)

        # Установка даты для группировки
        data.loc[data['Datetime'].dt.hour == expiration_hour, 'Date'] = data['Datetime'].dt.date
        data['Date'] = data['Date'].ffill()

        # Агрегация данных
        data_08_gb_hl = (
            data[['Date', 'High', 'Low', 'Volume']]
            .groupby('Date')
            .agg({'High': 'max', 'Low': 'min', 'Volume': 'sum'})
            .reset_index(drop=False)
        )
        data_08_gb_hl['Date'] = data_08_gb_hl['Date'] + timedelta(days=1)

        # Обработка данных на уровне 8:00
        data_08 = data[data['Datetime'].dt.hour == expiration_hour].copy()
        data_08['Datetime'] = data_08['Datetime'].dt.date
        data_08['Open_08'] = data_08['Open'].shift(1)
        data_08['Close_08'] = data_08['Open']

        # Объединение с агрегированными данными
        data_08 = (
            data_08[['Date', 'Open_08', 'Close_08']].set_index('Date')
            .join(data_08_gb_hl[['Date', 'High', 'Low', 'Volume']].set_index('Date'))
            .reset_index(drop=False).dropna()
            .rename(columns={'Open_08': 'Open', 'Close_08': 'Close'})
        )

        # Преобразование даты в datetime
        data_08['Date'] = pd.to_datetime(data_08['Date'])

        # Замена нулевых объемов на медианное значение
        data_08.loc[data_08['Volume'] == 0, 'Volume'] = data_08['Volume'].median()

        # Проверка наличия последней даты с 8-часовой свечой
        latest_date = data_08['Date'].max()
        expected_date = datetime.now(timezone.utc).date()

        if latest_date < pd.Timestamp(expected_date):
            latest_date_08_utc = latest_date.replace(
                hour=expiration_hour, minute=0, second=0, microsecond=0,
                tzinfo=timezone.utc
            )
            last_candles = data[data['Datetime'] >= latest_date_08_utc]

            if not last_candles.empty:
                # Рассчитываем данные для новой свечи
                new_row = pd.DataFrame([{
                    'Date': pd.Timestamp(expected_date),
                    'Open': last_candles['Open'].iloc[0],
                    'Close': last_candles['Close'].iloc[-1],
                    'High': last_candles['High'].max(),
                    'Low': last_candles['Low'].min(),
                    'Volume': last_candles['Volume'].sum(),
                    'Absolute_Body': abs(last_candles['Close'].iloc[-1] - last_candles['Open'].iloc[0]),
                }])

                data_08 = pd.concat([data_08, new_row], ignore_index=True)

        historical_volatility_window = self.config["historical_volatility_window"]
        trading_days_per_year = self.config["trading_days_per_year"]

        # Расчет ABS тела свечи
        data_08['Absolute_Body'] = abs(data_08['Close'] - data_08['Open'])

        # Расчет EWM
        data_08['Absolute_Body_EWM'] = data_08['Absolute_Body'].ewm(span=historical_volatility_window, adjust=False).mean()

        # Расчет средних и медианных значений ABS тела свечи
        data_08['Absolute_Body_Mean'] = data_08['Absolute_Body'].rolling(historical_volatility_window).mean()
        data_08['Absolute_Body_Median'] = data_08['Absolute_Body'].rolling(historical_volatility_window).median()

        # Расчет исторической волатильности
        data_08['Historical_Volatility'] = self.calculate_historical_volatility(
            data_08['Close'],
            historical_volatility_window,
            trading_days_per_year
        )

        # Кеширование данных
        self.data_08_cache[currency] = data_08
        return data_08

    def get_absolute_body_mean(self, currency: str, date: datetime) -> float:
        """
        Возвращает среднее абсолютного тела свечи.
        """
        data = self.get_data_08(currency, date)
        result = data[data['Date'] == date]
        return result['Absolute_Body_Mean'].values[0] if not result.empty else None

    def get_absolute_body_median(self, currency: str, date: datetime) -> float:
        """
        Возвращает медиану абсолютного тела свечи.
        """
        data = self.get_data_08(currency, date)
        result = data[data['Date'] == date]
        return result['Absolute_Body_Median'].values[0] if not result.empty else None

    def get_absolute_body_ewm(self, currency: str, date: datetime) -> float:
        """
        Возвращает EWM абсолютного тела свечи.
        """
        data = self.get_data_08(currency, date)
        result = data[data['Date'] == date]
        return result['Absolute_Body_EWM'].values[0] if not result.empty else None

    def calculate_historical_volatility(self, price_series, window, trading_days_per_year):
        """
        Рассчитывает историческую волатильность.

        :param price_series: pandas Series с ценами актива
        :param window: Окно для расчета волатильности (например, 252 для годовой волатильности)
        :param trading_days_per_year: Количество торговых дней в году (например, 365 для криптовалют)
        :return: Историческая волатильность (годовая)
        """
        log_returns = np.log(price_series / price_series.shift(1))
        volatility = log_returns.rolling(window=window).std() * np.sqrt(trading_days_per_year) * 100
        return volatility

    def get_historical_volatility(self, currency: str, date: datetime) -> float:
        """
        Получает историческую волатильность для указанной валюты и даты.

        :param currency: Валюта (например, 'BTC' или 'ETH').
        :param date: Дата, для которой требуется значение волатильности.
        :return: Историческая волатильность или None, если данные отсутствуют.
        """
        data = self.get_data_08(currency, date)
        result = data[data['Date'] == date]
        return result['Historical_Volatility'].values[0] if not result.empty else None
