import http from 'k6/http';
import { check, sleep } from 'k6';

// Fixed target and fixed workload: 1 VU, at most 20 iterations, two external
// requests per iteration. The token is deliberately invalid, not a credential.
const base = 'http://192.168.1.151';
const requestParams = {
  headers: { Authorization: 'Bearer codex-intentionally-invalid-k6-token' },
  responseCallback: http.expectedStatuses(401),
  timeout: '5s',
  redirects: 0,
};

export const options = {
  scenarios: { bounded: { executor: 'per-vu-iterations', vus: 1, iterations: 20, maxDuration: '90s' } },
  discardResponseBodies: true,
  thresholds: {
    checks: [{ threshold: 'rate==1', abortOnFail: true, delayAbortEval: '5s' }],
    http_req_failed: [{ threshold: 'rate==0', abortOnFail: true, delayAbortEval: '5s' }],
    http_req_duration: ['p(95)<1000'],
  },
};

export default function () {
  const security = http.post(`${base}/api/security/verify`, null, {
    ...requestParams,
    tags: { endpoint: 'security_verify' },
  });
  check(security, { 'security rejects invalid token': (r) => r.status === 401 });

  const uploader = http.get(`${base}/api/uploader/files`, {
    ...requestParams,
    tags: { endpoint: 'uploader_files_invalid_token' },
  });
  check(uploader, { 'uploader rejects invalid token': (r) => r.status === 401 });
  sleep(2);
}
