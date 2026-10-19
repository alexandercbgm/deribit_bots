import asyncio
import json
import websockets
import yaml
import requests
import sys
import time
import base64

from datetime import datetime, timedelta, timezone

# патч для asyncio
import nest_asyncio
nest_asyncio.apply()

# Глобальная переменная для имени файла конфигурации
CONFIG_PATH = "config/deribit_config.yaml"

class DeribitMixin:
    def __init__(self):
        # Загрузка конфигурации
        with open(CONFIG_PATH, "r") as config_file:
            config = yaml.safe_load(config_file)

        self.deribit_ws_connection_url: str = config["deribit_api_v2"]["ws_connection_url"]
        self.deribit_https_connection_url: str = config["deribit_api_v2"]["https_connection_url"]
        self.deribit_client_id: str = config["deribit_api_v2"]["client_id"]
        self.deribit_client_secret: str = config["deribit_api_v2"]["client_secret"]

        self.deribit_loop = asyncio.get_event_loop()
        self.deribit_websocket_client: websockets.WebSocketClientProtocol = None
        self.deribit_refresh_token: str = None
        self.deribit_refresh_token_expiry_time: int = None

        self.deribit_main_coins = config["main_coins"]
        self.deribit_currency_ids = config["currency_ids"]
        self.deribit_message_ids = config["message_ids"]
        self.deribit_alt_coins_request_counts_limit = config["alt_coins_request_counts_limit"]

        self.deribit_option_positions = {}
        self.deribit_option_instruments = {}
        self.deribit_option_prices_main_coins = {}
        self.deribit_option_prices_alt_coins = {}
        self.deribit_option_prices_alt_coins_request_counts = {}
        self.deribit_index_price = {}
        self.deribit_account_info = {}

    # Интерфейсный метод, который будет переопределяться в боте
    async def trading_logic(self, currency) -> None:
        pass

    # Запуск основного цикла
    def start_main_loop(self) -> None:
        self.deribit_loop.run_until_complete(self.ws_manager())

    async def ws_manager(self) -> None:
        async with websockets.connect(
            self.deribit_ws_connection_url,
            ping_interval=None,
            compression=None,
            close_timeout=60
        ) as self.deribit_websocket_client:

            await self.ws_auth()
            await self.establish_heartbeat()

            self.deribit_loop.create_task(self.ws_refresh_auth())
            self.deribit_loop.create_task(self.update_option_positions())
            self.deribit_loop.create_task(self.update_option_instruments())
            self.deribit_loop.create_task(self.update_option_prices_main_coins())
            self.deribit_loop.create_task(self.update_index_price())
            self.deribit_loop.create_task(self.update_account_info())

            while self.deribit_websocket_client.open:
                message: bytes = await self.deribit_websocket_client.recv()
                message: dict = json.loads(message)

                if "id" in message:
                    # refresh auth
                    if message["id"] == self.deribit_message_ids["ws_auth"]:
                        if self.deribit_refresh_token is None:
                            self.logger.info("Successfully authenticated WebSocket Connection")
                        else:
                            self.logger.info("Successfully refreshed the authentication of the WebSocket Connection")

                        self.deribit_refresh_token = message["result"]["refresh_token"]
                        expires_in = message["result"]["expires_in"] - 240
                        self.deribit_refresh_token_expiry_time = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

                    elif message["id"] == self.deribit_message_ids["heartbeat_response"]:
                        continue

                    elif "result" in message:
                        # account info proc answer
                        if message["id"] == self.deribit_message_ids["update_account_info"]:
                            self.deribit_account_info = message["result"]

                        for currency, currency_id in self.deribit_currency_ids.items():
                            # option instruments proc answer
                            if message["id"] == self.deribit_message_ids["update_option_instruments"] + currency_id:
                                self.deribit_option_instruments[currency] = message["result"]

                            # option prices main coins proc answer
                            elif message["id"] == self.deribit_message_ids["update_option_prices_main_coins"] + currency_id:
                                self.deribit_option_prices_main_coins[currency] = message["result"]

                            # option prices alt coins proc answer
                            elif message["id"] == self.deribit_message_ids["update_option_prices_alt_coins"] + currency_id:
                                if currency not in self.deribit_option_prices_alt_coins:
                                    self.deribit_option_prices_alt_coins[currency] = {}

                                instrument_name = message["result"][0]["instrument_name"]
                                self.deribit_option_prices_alt_coins[currency][instrument_name] = message["result"][0]

                            # currency index prices proc answer
                            elif message["id"] == self.deribit_message_ids["update_index_price"] + currency_id:
                                self.deribit_index_price[currency] = message["result"]["index_price"]

                            # option positions proc answer
                            elif message["id"] == self.deribit_message_ids["update_option_positions"] + currency_id:
                                self.deribit_option_positions[currency] = message["result"]

                                # При запуске бота ждем получения всех необходимых данных
                                if all([
                                    currency in self.deribit_option_positions,
                                    currency in self.deribit_option_instruments,
                                    currency in self.deribit_option_prices_main_coins,
                                    currency in self.deribit_index_price,
                                    self.deribit_account_info
                                ]):
                                    await self.trading_logic(currency)

                            # option buy proc answer
                            elif message["id"] == self.deribit_message_ids["buy_option"]:
                                self.send_telegram_message(message["result"], json_format=True)

                            # option sell proc answer
                            elif message["id"] == self.deribit_message_ids["sell_option"]:
                                self.send_telegram_message(message["result"], json_format=True)

                    elif "error" in message:
                        self.logger.debug(f"error in message: {message}")

                elif "method" in message and message["method"] == "heartbeat":
                    await self.heartbeat_response()

            else:
                self.logger.info("WebSocket connection has broken.")
                sys.exit(1)

    async def establish_heartbeat(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": self.deribit_message_ids["establish_heartbeat"],
            "method": "public/set_heartbeat",
            "params": {"interval": 10},
        }
        await self.deribit_websocket_client.send(json.dumps(msg))

    async def heartbeat_response(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": self.deribit_message_ids["heartbeat_response"],
            "method": "public/test",
            "params": {},
        }
        await self.deribit_websocket_client.send(json.dumps(msg))

    async def ws_auth(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": self.deribit_message_ids["ws_auth"],
            "method": "public/auth",
            "params": {
                "grant_type": "client_credentials",
                "client_id": self.deribit_client_id,
                "client_secret": self.deribit_client_secret,
            },
        }
        await self.deribit_websocket_client.send(json.dumps(msg))

    async def ws_refresh_auth(self) -> None:
        while True:
            if self.deribit_refresh_token_expiry_time and datetime.now(timezone.utc) > self.deribit_refresh_token_expiry_time:
                msg = {
                    "jsonrpc": "2.0",
                    "id": self.deribit_message_ids["ws_auth"],
                    "method": "public/auth",
                    "params": {
                        "grant_type": "refresh_token",
                        "refresh_token": self.deribit_refresh_token,
                    },
                }
                await self.deribit_websocket_client.send(json.dumps(msg))
            await asyncio.sleep(150)

    async def update_option_positions(self) -> None:
        while True:
            for currency, currency_id in self.deribit_currency_ids.items():
                msg = {
                    "method": "private/get_positions",
                    "params": {"currency": currency, "kind": "option"},
                    "jsonrpc": "2.0",
                    "id": self.deribit_message_ids["update_option_positions"] + currency_id,
                }
                await self.deribit_websocket_client.send(json.dumps(msg))
            await asyncio.sleep(5) # TEST
            # await asyncio.sleep(60)

    async def update_option_instruments(self) -> None:
        while True:
            for currency, currency_id in self.deribit_currency_ids.items():
                msg = {
                    "method": "public/get_instruments",
                    "params": {"currency": currency, "kind": "option"},
                    "jsonrpc": "2.0",
                    "id": self.deribit_message_ids["update_option_instruments"] + currency_id,
                }
                await self.deribit_websocket_client.send(json.dumps(msg))
            await asyncio.sleep(5)

    async def update_option_prices_main_coins(self) -> None:
        while True:
            for currency, currency_id in self.deribit_currency_ids.items():
                msg = {
                    "method": "public/get_book_summary_by_currency",
                    "params": {"currency": currency, "kind": "option"},
                    "jsonrpc": "2.0",
                    "id": self.deribit_message_ids["update_option_prices_main_coins"] + currency_id,
                }
                await self.deribit_websocket_client.send(json.dumps(msg))
            await asyncio.sleep(5)

    async def update_index_price(self) -> None:
        while True:
            for currency, currency_id in self.deribit_currency_ids.items():
                msg = {
                    "jsonrpc": "2.0",
                    "method": "public/get_index_price",
                    "id": self.deribit_message_ids["update_index_price"] + currency_id,
                    "params": {"index_name": f"{currency.lower()}_usd"},
                }
                await self.deribit_websocket_client.send(json.dumps(msg))
            await asyncio.sleep(5)

    async def update_account_info(self) -> None:
        while True:
            msg = {
                "jsonrpc": "2.0",
                "id": self.deribit_message_ids["update_account_info"],
                "method": "private/get_account_summaries",
                "params": {"extended": True},
            }
            await self.deribit_websocket_client.send(json.dumps(msg))
            await asyncio.sleep(5)

    async def buy_option(self, instrument_name: str, amount: float, price: float, order_type: str) -> None:
        """
        Отправка запроса на покупку опциона.

        :param instrument_name: Название инструмента.
        :param amount: Количество для покупки.
        :param price: Цена покупки.
        :param order_type: Тип ордера (например, "limit" или "market").
        """
        msg = {
            "method": "private/buy",
            "params": {
                "instrument_name": instrument_name,
                "amount": amount,
                "type": order_type,
                "price": price,
                "time_in_force": "good_til_cancelled",
                "label": "bot_buy",
            },
            "jsonrpc": "2.0",
            "id": self.deribit_message_ids["buy_option"],
        }

        # Если нужно отправлять сообщение в Telegram, раскомментируйте следующую строку
        # self.send_telegram_message(msg, json_format=True)

        await self.deribit_websocket_client.send(json.dumps(msg))
        await asyncio.sleep(5)

    async def sell_option(self, instrument_name: str, amount: float, price: float, order_type: str) -> None:
        """
        Отправка запроса на продажу опциона.

        :param instrument_name: Название инструмента.
        :param amount: Количество для продажи.
        :param price: Цена продажи.
        :param order_type: Тип ордера (например, "limit" или "market").
        """
        msg = {
            "method": "private/sell",
            "params": {
                "instrument_name": instrument_name,
                "amount": amount,
                "type": order_type,
                "price": price,
                "time_in_force": "good_til_cancelled",
                "label": "bot_sell",
            },
            "jsonrpc": "2.0",
            "id": self.deribit_message_ids["sell_option"],
        }

        # Если нужно отправлять сообщение в Telegram, раскомментируйте следующую строку
        # self.send_telegram_message(msg, json_format=True)

        await self.deribit_websocket_client.send(json.dumps(msg))
        await asyncio.sleep(5)

    def get_option_attr_main_coins(self, instrument_name: str, attr_name: str):
        """
        Получает значение указанного поля для опциона по имени инструмента (только BTC и ETH).

        :param instrument_name: Имя инструмента (например, "BTC-25NOV23-30000-C").
        :param attr_name: Название атрибута для извлечения (например, "ask_price", "bid_price").
        :return: Значение поля или None, если инструмент или поле не найдены.
        """
        currency = instrument_name.split('-')[0].split('_')[0]

        # Ищем в данных соответствующий инструмент
        for option in self.deribit_option_prices_main_coins.get(currency, []):
            if option["instrument_name"] == instrument_name:
                return option.get(attr_name), 1

        return None, None

    async def get_option_attr_alt_coins(self, instrument_name: str, attr_name: str) -> None:
        """
        Асинхронный запрос данных об атрибуте опциона через WebSocket API.

        :param instrument_name: Название инструмента.
        :param attr_name: Название атрибута (например, "ask_price", "bid_price").
        :return: Значение атрибута или None, если данные не найдены.
        """
        currency = instrument_name.split('-')[0].split('_')[0]

        msg = {
            "method": "public/get_book_summary_by_instrument",
            "params": {"instrument_name": instrument_name},
            "jsonrpc": "2.0",
            "id": self.deribit_message_ids["update_option_prices_alt_coins"] + self.deribit_currency_ids[currency],
        }

        # Если после n запросов нет ответа по instrument_name, значит такого страйка не существует
        self.deribit_option_prices_alt_coins_request_counts.setdefault(currency, {}).setdefault(instrument_name, 0)
        self.deribit_option_prices_alt_coins_request_counts[currency][instrument_name] += 1

        await self.deribit_websocket_client.send(json.dumps(msg))

    async def get_option_attr(self, instrument_name: str, attr_name: str):
        """
        Получение атрибута опциона по имени инструмента.

        :param instrument_name: Название инструмента (например, "BTC-25NOV23-30000-C").
        :param attr_name: Название атрибута (например, "ask_price", "bid_price").
        :return: Значение атрибута или None, если данные отсутствуют, и флаг отсутствия полученных данных.
        """
        currency = instrument_name.split('-')[0].split('_')[0]

        if currency in self.deribit_main_coins:
            return self.get_option_attr_main_coins(instrument_name, attr_name)
        else:
            await self.get_option_attr_alt_coins(instrument_name, attr_name)

            if currency in self.deribit_option_prices_alt_coins and \
                    instrument_name in self.deribit_option_prices_alt_coins[currency]:
                return self.deribit_option_prices_alt_coins[currency][instrument_name][attr_name], 1
            else:
                if self.deribit_option_prices_alt_coins_request_counts.get(currency, {}).get(instrument_name, 0) == 0:
                    self.logger.debug(f"Первый запрос данных по alt coin: {instrument_name}")

        return None, None

    def clear_option_prices(self, currency):
        """
        Очищает цены опционов для указанной валюты.

        Очистка применяется только к альтернативным монетам, так как цены основных монет 
        обновляются в цикле.

        :param currency: Валюта для очистки цен.
        """
        if currency not in self.deribit_main_coins:
            self.deribit_option_prices_alt_coins[currency] = {}
            self.logger.debug("Цены очищены для %s.", currency)
