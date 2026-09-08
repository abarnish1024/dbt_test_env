select
  results.result_id,
  results.race_id,
  results.driver_id,
  races.race_name,
  races.race_year,
  races.race_date,
  drivers.driver_name,
  drivers.nationality as driver_nationality,
  results.grid,
  results.finish_position,
  results.points as race_points,
  results.laps as race_laps
from {{ ref('stg_results') }} as results
left join {{ ref('stg_races') }} as races
  on results.race_id = races.race_id
left join {{ ref('stg_drivers') }} as drivers
  on results.driver_id = drivers.driver_id
