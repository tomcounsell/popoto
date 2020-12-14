const JAN_1_2017_TIMESTAMP = 1483228800

function timestamp_from_score(score){
  return (score * 300) + JAN_1_2017_TIMESTAMP
}

function score_from_timestamp(timestamp){
  return Math.round((timestamp - JAN_1_2017_TIMESTAMP) / 300)
}

function datetime_from_score(score){
  return new Date(timestamp_from_score(score))
}

function seconds_from_periods(periods){
  return Math.round(periods * 300)
}

function periods_from_seconds(seconds){
  return seconds / 300
}
