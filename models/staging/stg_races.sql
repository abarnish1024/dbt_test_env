select
  "raceId" as race_id,
  "year" as race_year,
  "round" as race_round,
  "circuitId" as circuit_id,
  "name" as race_name,
  "date" as race_date,
  "url" as race_url
from {{ source('f1', 'races') }}
