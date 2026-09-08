select
  "driverId" as driver_id,
  "driverRef" as driver_ref,
  "code" as driver_code,
  "forename" as forename,
  "surname" as surname,
  trim(coalesce("forename", '') || ' ' || coalesce("surname", '')) as driver_name,
  "dob" as date_of_birth,
  "nationality" as nationality
from {{ source('f1', 'drivers') }}
