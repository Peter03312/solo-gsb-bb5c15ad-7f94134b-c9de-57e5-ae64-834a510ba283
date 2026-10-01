from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 默认值即 docker-compose 中的连接串，可用环境变量覆盖
    database_url: str = (
        "postgresql://royalty:royalty@db:5432/royalty"
    )
    db_min_size: int = 1
    db_max_size: int = 10
    # 小数比例最多保留的位数（百分比，4 位 = 0.0001%）
    percent_scale: int = 4

    class Config:
        env_file = ".env"
        env_prefix = ""


settings = Settings()
