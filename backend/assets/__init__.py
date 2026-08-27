"""IT asset persistence package.

泛型资产模型（借鉴 GLPI）：资产类型用 asset_type 文本字段区分，
每类资产的差异化字段放 custom_fields JSON，不为一类资产单建表。
"""

from .models import AssetRecord, AssetStatus, CreateAsset, UpdateAsset
from .repository import AssetAlreadyExists, AssetNotFound, AssetRepository

__all__ = [
    "AssetAlreadyExists",
    "AssetNotFound",
    "AssetRecord",
    "AssetRepository",
    "AssetStatus",
    "CreateAsset",
    "UpdateAsset",
]
