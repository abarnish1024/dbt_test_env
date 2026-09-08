select
  driver_id,
  driver_ref,
  driver_code,
  driver_name,
  date_of_birth,
  nationality
from {{ ref('stg_drivers') }}
