from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "factory"
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    # 启动时是否自动灌演示数据（orders 表为空才灌）。默认关闭：
    # 开着的话任何一次清空数据库都会在下次重启/热重载时被 24 张演示工单覆盖回来。
    # 需要演示数据时在 .env 里写 AUTO_SEED=true。
    auto_seed: bool = False

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "factory_erp"

    wx_appid: str = ""
    wx_app_secret: str = ""
    mp_mock_phone: str = ""

    ship_from_company: str = "苏州展晟达精密科技有限公司"

    @property
    def database_url(self) -> str:
        return f"mysql+pymysql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
