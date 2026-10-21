import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (file: string) => readFileSync(new URL(file, import.meta.url), 'utf8');

describe('LemonCrow radius system', () => {
  it('maps regular controls to 6px and panels to 12px', () => {
    expect(read('../../index.css')).toContain('--radius-control: 6px;');
    expect(read('../../index.css')).toContain('--radius-panel: 12px;');
    expect(read('../../../tailwind.config.ts')).toContain("md: 'var(--radius-control)'");
    expect(read('../../../tailwind.config.ts')).toContain("lg: 'var(--radius-panel)'");
    expect(read('./button.tsx')).toContain('rounded-md');
    expect(read('./input.tsx')).toContain('rounded-md');
    expect(read('./card.tsx')).toContain('rounded-lg');
  });
  it('keeps review state badges on a 6px token', () => {
    expect(read('../../review/reviewUi.css')).toContain('border-radius: var(--radius-badge);');
  });
});
