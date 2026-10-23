export const version = '0.1.0';

export function healthLabel(status: 'ok' | 'error'): string {
  return status === 'ok' ? 'Healthy' : 'Unavailable';
}
