import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


def main() -> None:
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)

    # Load local plugins from src/plugins
    nonebot.load_plugins("src/plugins")

    nonebot.run()


if __name__ == "__main__":
    main()
