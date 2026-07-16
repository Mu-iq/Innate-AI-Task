/**
 * Mirrors backend/app/schemas.py. Kept hand-written rather than generated:
 * the shapes are small and stable, and a codegen step would be one more thing
 * to install before the demo renders.
 */

export type ImageSource = 'streetview' | 'places_photo';
export type Verdict = 'accept' | 'reject';
export type Stage = 'discover' | 'capture' | 'assess' | 'measure' | 'composite' | 'verify';
export type FrontagePalette = 'dark' | 'light' | 'warm_brick' | 'mixed';

/**
 * "decision" — the pipeline looked and said no. These are the deliverable: the
 *              evidence that selection was automated.
 * "error"    — the pipeline never got to decide (quota, retired model, network).
 *              Operational noise, shown separately so it cannot be mistaken for
 *              judgement the system never actually made.
 */
export type RejectionKind = 'decision' | 'error';

export interface Rejection {
  venue_id: string;
  venue_name: string;
  address: string;
  stage: Stage;
  kind: RejectionKind;
  reasons: string[];
  detail: string;
  at: string;
}

export interface Assessment {
  entrance_visible: boolean;
  frontage_bare_score: number;
  framing_usable: boolean;
  frontage_palette: FrontagePalette;
  people_prominence: number;
  obstructions: string[];
  reject_reasons: string[];
  accepted: boolean;
  product_slug: string;
}

export interface Measurement {
  door_bbox: number[];
  door_height_px: number;
  ground_line_y: number;
  light_direction: string;
  placement_zones: number[][];
  px_per_metre: number;
  expected_planter_px: number;
  product_slug: string;
}

export interface Verification {
  building_unaltered: boolean;
  product_faithful_to_reference: boolean;
  scale_plausible: boolean;
  grounded_with_shadow: boolean;
  planter_blocks_entrance: boolean;
  observed_planter_height_px: number | null;
  verdict: Verdict;
  reject_reasons: string[];
  scale_ratio: number | null;
  scale_within_tolerance: boolean | null;
}

export interface VenueResult {
  id: string;
  name: string;
  address: string;
  postcode: string;
  lat: number;
  lng: number;
  area: string;
  image_source: ImageSource;
  heading_used: number | null;
  pano_distance_m: number | null;
  product_slug: string;
  product_description: string;
  assessment: Assessment;
  measurement: Measurement;
  verification: Verification;
  frontage_url: string;
  composite_url: string;
  attempts: number;
}

export interface Funnel {
  discovered: number;
  after_chain_filter: number;
  after_status_filter: number;
  entered_pipeline: number;
  capture_ok: number;
  assess_ok: number;
  measure_ok: number;
  composite_ok: number;
  accepted: number;
}

/**
 * The constants the run's decisions were made against, shipped in results.json.
 * The UI renders these rather than hardcoding "≥ 6" and "±40%", so tuning
 * config.py can never leave the interface describing a bar that no longer exists.
 */
export interface Thresholds {
  frontage_bare_threshold: number;
  standard_door_height_m: number;
  scale_tolerance: number;
  max_composite_attempts: number;
  max_pano_distance_m: number;
  people_prominence_threshold: number;
  heading_nudge_deg: number;
}

export interface RunStatus {
  run_id: string;
  stage: string;
  processed: number;
  accepted: number;
  rejected: number;
  done: boolean;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

/** Where a payload came from. Always shown, never inferred. */
export type PayloadSource = 'database' | 'snapshot';

export interface ResultsPayload {
  run_id: string;
  generated_at: string;
  dry_run: boolean;
  vision_model: string;
  image_model: string;
  thresholds: Thresholds | null;
  settings: RunSettings | null;
  cost: RunCost | null;
  funnel: Funnel;
  venues: VenueResult[];
  rejected: Rejection[];
  source: PayloadSource;
}

/** One row of run history. Only available when a database is configured. */
export interface RunCost {
  counts: Record<string, number>;
  cost_usd: Record<string, number>;
  total_cost_usd: number;
}

export interface RunSettings {
  max_venues: number;
  target_accepted: number;
}

export interface RunSummary {
  run_key: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  stage: string;
  started_at: string;
  finished_at: string | null;
  duration_s: number | null;
  dry_run: boolean;
  vision_model: string;
  image_model: string;
  max_venues: number | null;
  target_accepted: number | null;
  total_cost_usd: number;
  discovered: number;
  entered_pipeline: number;
  accepted: number;
  // Counted apart. A run that errored on every venue is not a run that rejected
  // every venue, and a history list that conflates them is lying.
  rejected_decisions: number;
  rejected_errors: number;
  error: string | null;
}
