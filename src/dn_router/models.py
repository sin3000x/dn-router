# 输入结构与业务字段校验。所有模型使用 BaseModel 默认行为：忽略额外字段。
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal
from pydantic import BaseModel, Field, model_validator, field_validator

# 使用 Decimal 保留业务数值精度；拒绝 NaN 和无穷大。
NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


# 配送单：DN 是不可拆分的配送单位，siteCode 标识实际卸货地点。
class DN(BaseModel):
    orderReleaseId: str
    siteCode: str
    deliveryLatitude: Annotated[Decimal, Field(ge=-90, le=90, allow_inf_nan=False)]
    deliveryLongitude: Annotated[Decimal, Field(ge=-180, le=180, allow_inf_nan=False)]
    totalGrossVolume: NonNegative
    totalGrossWeight: NonNegative
    timeWindow: list[datetime] | None = None

    @field_validator("timeWindow")
    @classmethod
    def window(cls, value):
        # 这里只检查时间窗结构；空窗和倒置窗口由预处理按业务规则剔除。
        if value is not None and len(value) not in (0, 2):
            raise ValueError("timeWindow must be null, [], or [start, end]")
        # 采用业务当地时间，拒绝时区和小数秒，避免计算口径不一致。
        if value and any(x.tzinfo is not None or x.microsecond for x in value):
            raise ValueError("use local timestamps without timezone or fractional seconds")
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
    unloadingTime: NonNegative = Decimal("0")
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
    siteDistanceLimit: Annotated[Decimal, Field(allow_inf_nan=False)] = Decimal("0")
    sourceLatitude: Annotated[Decimal, Field(ge=-90, le=90, allow_inf_nan=False)]
    sourceLongitude: Annotated[Decimal, Field(ge=-180, le=180, allow_inf_nan=False)]
    sourceLocation: str
    taskNo: str = ""
    vehicleTypeVoList: list[Vehicle] = Field(min_length=1)

    @model_validator(mode="after")
    def consistency(self):
        # DN ID 用于矩阵索引，不能重复或与仓库重名；车型名称也必须唯一。
        ids = [x.orderReleaseId for x in self.dnSiteVoList]
        names = [x.vehicleTypeName for x in self.vehicleTypeVoList]
        if len(set(ids)) != len(ids) or self.sourceLocation in ids or any(not x for x in ids):
            raise ValueError("DN IDs must be nonempty, unique, and distinct from sourceLocation")
        if len(set(names)) != len(names) or any(not x for x in names):
            raise ValueError("vehicleTypeName must be nonempty and unique")
        # 出发时间同样要求整秒当地时间，且出发上限不得早于下限。
        for date in (self.earliestShippingDate, self.latestShippingDate):
            if date and (date.tzinfo is not None or date.microsecond):
                raise ValueError("shipping times must be local, whole-second timestamps")
        if self.latestShippingDate and self.latestShippingDate < self.earliestShippingDate:
            raise ValueError("warehouse departure window is empty")
        return self


# 生成站点计数键，与按 siteCode 合并实际卸货停靠的规则独立。
def group_key(dn: DN, mode: int | None):
    if mode == 0:
        return ("site", dn.siteCode)
    if mode == 1:
        # 明确使用四舍五入，保证负坐标及临界小数的分组规则一致。
        q = Decimal("0.000001")
        return ("geo", dn.deliveryLatitude.quantize(q, rounding=ROUND_HALF_UP),
                dn.deliveryLongitude.quantize(q, rounding=ROUND_HALF_UP))
    return ("dn", dn.orderReleaseId)
