export interface StreamCustomMetadata<TStep extends object, TNl2a> {
  [key: string]: unknown;
  stepTokenCounts?: TStep[];
  nl2a?: TNl2a;
}

/**
 * Builds an immutable per-message snapshot of streaming custom metadata.
 * Step token entries are flat value objects, so cloning every entry is enough
 * to detach completed messages from the mutable accumulator owned by a run.
 */
export function buildStreamCustomMetadata<TStep extends object, TNl2a = never>(
  steps: readonly TStep[],
  nl2a?: TNl2a
): StreamCustomMetadata<TStep, TNl2a> | undefined {
  if (steps.length === 0 && nl2a === undefined) return undefined;

  return {
    ...(steps.length > 0
      ? { stepTokenCounts: steps.map((step) => ({ ...step })) }
      : {}),
    ...(nl2a !== undefined ? { nl2a } : {}),
  };
}
