import yaml

# Глобальная переменная для имени файла конфигурации
CONFIG_FILE = "config/config.yaml"

class ConfigMixin:
    def __init__(self):
        self._config = self._load_config()

    @staticmethod
    def _load_config():
        """
        Загружает конфигурацию из YAML-файла.
        """
        try:
            with open(CONFIG_FILE, "r") as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            raise FileNotFoundError(f"Конфигурационный файл '{CONFIG_FILE}' не найден.")
        except yaml.YAMLError as e:
            raise ValueError(f"Ошибка при чтении YAML-файла: {e}")

    def get_config_value(self, *keys):
        """
        Рекурсивно получает значение из конфигурации по указанным ключам.
        :param keys: Путь к значению в конфигурации.
        :return: Значение из конфигурации.
        :raises KeyError: Если ключ не найден.
        """
        config = self._config
        for key in keys:
            if isinstance(config, dict) and key in config:
                config = config[key]
            else:
                raise KeyError(f"Key {' -> '.join(keys)} not found in configuration.")
        return config
