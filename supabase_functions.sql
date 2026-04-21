-- Run this in your Supabase SQL Editor AFTER supabase_schema.sql.
-- Creates a single RPC that the status page calls instead of fetching raw rows.
-- Replaces ~50 paginated REST calls with one fast server-side aggregation.

CREATE OR REPLACE FUNCTION get_status_summary(
  check_interval_min int DEFAULT 10,
  max_bar_slots int DEFAULT 144
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  svc_keys text[] := ARRAY['reachability','sampling','openai_compatible','training_infra'];
  win_hours int[] := ARRAY[24, 168, 720, 2160];
  win_keys text[] := ARRAY['24h','7d','30d','90d'];
  since_90d timestamptz := now() - interval '90 days';

  ticks_out jsonb := '{}'::jsonb;
  uptime_out jsonb := '{}'::jsonb;
  latency_out jsonb := '{}'::jsonb;
  latest_out jsonb := '{}'::jsonb;
  incidents_out jsonb;

  s text;
  w_idx int;
  w_start timestamptz;
  raw_ticks int;
  num_ticks int;
  bkt_secs double precision;
  tick_arr text[];
  up_ct int;
  total_ct int;
  earliest_ts timestamptz;
BEGIN
  -- 1) Latest status per service
  FOR s IN SELECT unnest(svc_keys) LOOP
    latest_out := latest_out || jsonb_build_object(s,
      (SELECT jsonb_build_object(
         'service', service, 'status', status,
         'checked_at', checked_at, 'error', error, 'latency_ms', latency_ms)
       FROM check_results WHERE service = s ORDER BY checked_at DESC LIMIT 1));
  END LOOP;

  -- 2) Ticks + uptime per service per window
  FOR s IN SELECT unnest(svc_keys) LOOP
    ticks_out := ticks_out || jsonb_build_object(s, '{}'::jsonb);
    uptime_out := uptime_out || jsonb_build_object(s, '{}'::jsonb);

    FOR w_idx IN 1..4 LOOP
      w_start := now() - (win_hours[w_idx] || ' hours')::interval;
      raw_ticks := round((win_hours[w_idx] * 60.0) / check_interval_min);
      num_ticks := LEAST(raw_ticks, max_bar_slots);
      bkt_secs := extract(epoch FROM now() - w_start) / num_ticks;

      -- Tick array: bucket each row, pick worst status per bucket
      SELECT array_agg(
        CASE
          WHEN has_down THEN 'down'
          WHEN has_up THEN 'up'
          ELSE 'empty'
        END ORDER BY idx
      ) INTO tick_arr
      FROM (
        SELECT
          b.idx,
          bool_or(cr.status = 'down') AS has_down,
          bool_or(cr.status = 'up') AS has_up
        FROM generate_series(0, num_ticks - 1) AS b(idx)
        LEFT JOIN check_results cr
          ON cr.service = s
         AND cr.checked_at >= w_start
         AND LEAST(floor(extract(epoch FROM cr.checked_at - w_start) / bkt_secs)::int, num_ticks - 1) = b.idx
        GROUP BY b.idx
      ) bucketed;

      ticks_out := jsonb_set(ticks_out, ARRAY[s],
        (ticks_out->s) || jsonb_build_object(win_keys[w_idx], to_jsonb(tick_arr)));

      -- Uptime %
      SELECT count(*) FILTER (WHERE status = 'up'), count(*), min(checked_at)
        INTO up_ct, total_ct, earliest_ts
        FROM check_results WHERE service = s AND checked_at >= w_start;

      IF total_ct = 0 OR extract(epoch FROM now() - earliest_ts) * 1000 < win_hours[w_idx] * 3600000.0 * 0.9 THEN
        uptime_out := jsonb_set(uptime_out, ARRAY[s],
          (uptime_out->s) || jsonb_build_object(win_keys[w_idx], null));
      ELSE
        uptime_out := jsonb_set(uptime_out, ARRAY[s],
          (uptime_out->s) || jsonb_build_object(win_keys[w_idx], round(up_ct * 100.0 / total_ct, 2)));
      END IF;
    END LOOP;
  END LOOP;

  -- 3) Latency percentiles (90d)
  FOR s IN SELECT unnest(svc_keys) LOOP
    latency_out := latency_out || jsonb_build_object(s,
      (SELECT jsonb_build_object(
         'p50', round(percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)::numeric, 1),
         'p95', round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::numeric, 1),
         'p99', round(percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms)::numeric, 1))
       FROM check_results
       WHERE service = s AND latency_ms IS NOT NULL AND checked_at >= since_90d));
  END LOOP;

  -- 4) Incidents: just the down rows (small set, client merges them)
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'service', service, 'error', error, 'checked_at', checked_at
  ) ORDER BY checked_at), '[]'::jsonb)
  INTO incidents_out
  FROM check_results WHERE status = 'down' AND checked_at >= since_90d;

  RETURN jsonb_build_object(
    'ticks', ticks_out,
    'uptime', uptime_out,
    'latency', latency_out,
    'latest', latest_out,
    'incidents', incidents_out
  );
END;
$$;

GRANT EXECUTE ON FUNCTION get_status_summary(int, int) TO anon;
