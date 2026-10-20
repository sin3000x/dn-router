from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal
from pydantic import BaseModel, Field, model_validator, field_validator

NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


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
        if value is not None and len(value) not in (0, 2):
            raise ValueError("timeWindow must be null, [], or [start, end]")
        if value and any(x.tzinfo is not None or x.microsecond for x in value):
            raise ValueError("use local timestamps without timezone or fractional seconds")
        return value


class Vehicle(BaseModel):
    themeCode: str | None = None
    totalVolume: Positive
    totalWeight: Positive
    vehicleTypeCode: str | None = None
    vehicleTypeName: str


class Leg(BaseModel):
    beginSiteCode: str | None = None
    endSiteCode: str | None = None
    origin: str = Field(alias="from")
    to: str
    planDistance: NonNegative
    duration: Annotated[int, Field(ge=0)] | None = None


class Request(BaseModel):
    allPathDistanceVoList: list[Leg]
    pathDistanceVoList: list[Leg] = Field(default_factory=list)
    unloadingTime: NonNegative = Decimal("0")
    earliestShippingDate: datetime
    latestShippingDate: datetime | None = None
    enableTimeWindows: bool = True
    destCountryCode3: str | None = None
    distanceLessThanFirstLeg: bool = False
    dnSiteVoList: list[DN]
    lonAndLatCombination: Literal[0, 1, 2] | None = None
    maxSiteQty: Annotated[int, Field(ge=0)] = 0
    quotationModel: Literal["distance", "city", "zipCode", "province", ""] | None = None
    siteDistanceLimit: Annotated[Decimal, Field(allow_inf_nan=False)] = Decimal("0")
    sourceLatitude: Annotated[Decimal, Field(ge=-90, le=90, allow_inf_nan=False)]
    sourceLongitude: Annotated[Decimal, Field(ge=-180, le=180, allow_inf_nan=False)]
    sourceLocation: str
    taskNo: str = ""
    vehicleTypeVoList: list[Vehicle] = Field(min_length=1)

    @model_validator(mode="after")
    def consistency(self):
        ids = [x.orderReleaseId for x in self.dnSiteVoList]
        names = [x.vehicleTypeName for x in self.vehicleTypeVoList]
        if len(set(ids)) != len(ids) or self.sourceLocation in ids or any(not x for x in ids):
            raise ValueError("DN IDs must be nonempty, unique, and distinct from sourceLocation")
        if len(set(names)) != len(names) or any(not x for x in names):
            raise ValueError("vehicleTypeName must be nonempty and unique")
        for date in (self.earliestShippingDate, self.latestShippingDate):
            if date and (date.tzinfo is not None or date.microsecond):
                raise ValueError("shipping times must be local, whole-second timestamps")
        if self.latestShippingDate and self.latestShippingDate < self.earliestShippingDate:
            raise ValueError("warehouse departure window is empty")
        return self


def group_key(dn: DN, mode: int | None):
    if mode == 0:
        return ("site", dn.siteCode)
    if mode == 1:
        q = Decimal("0.000001")
        return ("geo", dn.deliveryLatitude.quantize(q, rounding=ROUND_HALF_UP),
                dn.deliveryLongitude.quantize(q, rounding=ROUND_HALF_UP))
    return ("dn", dn.orderReleaseId)
