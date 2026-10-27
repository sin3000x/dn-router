# 输入结构与业务字段校验。所有模型使用 BaseModel 默认行为：忽略额外字段。
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

# 使用 Decimal 保留业务数值精度；拒绝 NaN 和无穷大。
NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Latitude = Annotated[Decimal, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[Decimal, Field(ge=-180, le=180, allow_inf_nan=False)]

GroupKey = tuple[str | Decimal, ...]

# 这些报价模式以车辆数为第一目标，distance/null/空串以总距离为第一目标。
COUNT_FIRST_QUOTATIONS = frozenset({"city", "zipCode", "province"})
_MICRO = Decimal("0.000001")


def _is_local_whole_second(value: datetime) -> bool:
    """采用业务当地时间，拒绝时区和小数秒，避免计算口径不一致。"""
    return value.tzinfo is None and not value.microsecond


# 配送单：DN 是不可拆分的配送单位，siteCode 标识实际卸货地点。
class DN(BaseModel):
    orderReleaseId: str
    siteCode: str
    deliveryLatitude: Latitude
    deliveryLongitude: Longitude
    totalGrossVolume: NonNegative
    totalGrossWeight: NonNegative
    timeWindow: list[datetime] | None = None

    @field_validator("timeWindow")
    @classmethod
    def _window(cls, value: list[datetime] | None) -> list[datetime] | None:
        # 这里只检查时间窗结构；空窗和倒置窗口由预处理按业务规则剔除。
        if value is not None and len(value) not in (0, 2):
            raise ValueError("timeWindow must be null, [], or [start, end]")
        if value and not all(map(_is_local_whole_second, value)):
            raise ValueError(
                "use local timestamps without timezone or fractional seconds"
            )
        return value


# 车型容量必须为正；车型名称作为业务唯一标识，数量不设上限。
class Vehicle(BaseModel):
    themeCode: str | None = None
    totalVolume: Positive
    totalWeight: Positive
    vehicleTypeCode: str | None = None
    vehicleTypeName: str


# 有向矩阵边；from 是 Python 关键字，内部通过 origin 访问。
class Leg(BaseModel):
    beginSiteCode: str | None = None
    endSiteCode: str | None = None
    origin: str = Field(alias="from")
    to: str
    planDistance: NonNegative
    duration: Annotated[int, Field(ge=0)] | None = None


# 配送请求：矩阵、订单、车型及路线约束集中在这里声明。
class Request(BaseModel):
    # 唯一权威矩阵；pathDistanceVoList 仅兼容接收。
    allPathDistanceVoList: list[Leg]
    pathDistanceVoList: list[Leg] = Field(default_factory=list)
    # 卸货单位为小时；出发窗口采用同一业务当地时间。
    unloadingTime: NonNegative = Decimal(0)
    earliestShippingDate: datetime
    latestShippingDate: datetime | None = None
    enableTimeWindows: bool = True
    destCountryCode3: str | None = None
    distanceLessThanFirstLeg: bool = False
    dnSiteVoList: list[DN]
    # 站点计数：0 按站点，1 按六位小数坐标，2/null 按 DN；数量 0 表示不限。
    lonAndLatCombination: Literal[0, 1, 2] | None = None
    maxSiteQty: Annotated[int, Field(ge=0)] = 0
    # distance/null/空串优先距离，其余枚举优先车辆数；不读取报价价格。
    quotationModel: Literal["distance", "city", "zipCode", "province", ""] | None = None
    # 仅限制配送点之间的单段距离，非正数表示不限。
    siteDistanceLimit: Annotated[Decimal, Field(allow_inf_nan=False)] = Decimal(0)
    sourceLatitude: Latitude
    sourceLongitude: Longitude
    sourceLocation: str
    taskNo: str = ""
    vehicleTypeVoList: list[Vehicle] = Field(min_length=1)

    @property
    def count_first(self) -> bool:
        return self.quotationModel in COUNT_FIRST_QUOTATIONS

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        # DN ID 用于矩阵索引，不能重复或与仓库重名；车型名称也必须唯一。
        ids = [x.orderReleaseId for x in self.dnSiteVoList]
        names = [x.vehicleTypeName for x in self.vehicleTypeVoList]
        if len(set(ids)) != len(ids) or self.sourceLocation in ids or not all(ids):
            raise ValueError(
                "DN IDs must be nonempty, unique, and distinct from sourceLocation"
            )
        if len(set(names)) != len(names) or not all(names):
            raise ValueError("vehicleTypeName must be nonempty and unique")
        # 出发时间同样要求整秒当地时间，且出发上限不得早于下限。
        dates = [d for d in (self.earliestShippingDate, self.latestShippingDate) if d]
        if not all(map(_is_local_whole_second, dates)):
            raise ValueError("shipping times must be local, whole-second timestamps")
        if (
            self.latestShippingDate
            and self.latestShippingDate < self.earliestShippingDate
        ):
            raise ValueError("warehouse departure window is empty")
        return self


def group_key(dn: DN, mode: int | None) -> GroupKey:
    """生成站点计数键，与按 siteCode 合并实际卸货停靠的规则独立。

    Args:
        dn: 配送单。
        mode: 请求的 lonAndLatCombination。

    Returns:
        mode 为 0 时返回 ("site", siteCode)；为 1 时返回 ("geo", 纬度, 经度)，
        坐标四舍五入到六位小数；其余返回 ("dn", orderReleaseId)。
    """
    if mode == 0:
        return ("site", dn.siteCode)
    if mode == 1:
        # 明确使用四舍五入，保证负坐标及临界小数的分组规则一致。
        return (
            "geo",
            dn.deliveryLatitude.quantize(_MICRO, rounding=ROUND_HALF_UP),
            dn.deliveryLongitude.quantize(_MICRO, rounding=ROUND_HALF_UP),
        )
    return ("dn", dn.orderReleaseId)
