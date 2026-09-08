select
  "resultId" as result_id,
  "raceId" as race_id,
  "driverId" as driver_id,
  "constructorId" as constructor_id,
  "grid" as grid,
  try_cast("position" as integer) as finish_position,
  "positionOrder" as position_order,
  "points" as points,
  "laps" as laps,
  "statusId" as status_id
from {{ source('f1', 'results') }}
