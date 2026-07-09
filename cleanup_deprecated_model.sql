-- Run this in your Supabase SQL Editor to clean up ~3,136 false outage rows
-- caused by Llama-3.2-1B being deprecated on June 13, 2026.
--
-- These are NOT real outages — reachability was 100% the entire time.
-- The sampling, openai_compatible, and training_infra checks failed because
-- they hardcoded a model that Tinker retired.

-- Delete all fake deprecation-caused down rows since June 13
DELETE FROM check_results
WHERE status = 'down'
  AND checked_at >= '2026-06-13T00:00:00Z'
  AND service IN ('sampling', 'openai_compatible', 'training_infra')
  AND (
    error ILIKE '%Llama-3.2-1B%'
    OR error ILIKE '%400 Bad Request%/completions%'
  );
