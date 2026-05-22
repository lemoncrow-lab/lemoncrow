// Synthetic A/B fixture — TypeScript HTTP client with retry logic.
// NOT for production use. Generated to exercise tree-sitter outline.

import { EventEmitter } from "events";

export interface RetryOptions {
  maxAttempts: number;
  baseDelayMs: number;
  maxDelayMs: number;
  jitter: boolean;
}

export interface RequestOptions {
  method: "GET" | "POST" | "PUT" | "DELETE" | "PATCH";
  headers?: Record<string, string>;
  body?: unknown;
  timeout?: number;
  retry?: Partial<RetryOptions>;
}

export interface Response<T> {
  status: number;
  headers: Record<string, string>;
  data: T;
  latencyMs: number;
}

export class HttpError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: string,
    message?: string,
  ) {
    super(message ?? `HTTP ${status}`);
    this.name = "HttpError";
  }
}

export class RetryExhaustedError extends Error {
  constructor(
    public readonly attempts: number,
    public readonly lastError: Error,
  ) {
    super(`Retried ${attempts} times, last error: ${lastError.message}`);
    this.name = "RetryExhaustedError";
  }
}

const DEFAULT_RETRY: RetryOptions = {
  maxAttempts: 3,
  baseDelayMs: 100,
  maxDelayMs: 5000,
  jitter: true,
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function computeDelay(attempt: number, opts: RetryOptions): number {
  const exp = Math.min(
    opts.baseDelayMs * Math.pow(2, attempt),
    opts.maxDelayMs,
  );
  return opts.jitter ? exp * (0.5 + Math.random() * 0.5) : exp;
}

function isRetryable(status: number): boolean {
  return status === 429 || status === 503 || status >= 500;
}

export class HttpClient extends EventEmitter {
  private readonly baseUrl: string;
  private readonly defaultHeaders: Record<string, string>;
  private readonly defaultRetry: RetryOptions;
  private abortController: AbortController | null = null;

  constructor(
    baseUrl: string,
    options: {
      headers?: Record<string, string>;
      retry?: Partial<RetryOptions>;
    } = {},
  ) {
    super();
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.defaultHeaders = {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...options.headers,
    };
    this.defaultRetry = { ...DEFAULT_RETRY, ...options.retry };
  }

  async get<T>(
    path: string,
    opts?: Omit<RequestOptions, "method" | "body">,
  ): Promise<Response<T>> {
    return this.request<T>(path, { ...opts, method: "GET" });
  }

  async post<T>(
    path: string,
    body: unknown,
    opts?: Omit<RequestOptions, "method">,
  ): Promise<Response<T>> {
    return this.request<T>(path, { ...opts, method: "POST", body });
  }

  async put<T>(
    path: string,
    body: unknown,
    opts?: Omit<RequestOptions, "method">,
  ): Promise<Response<T>> {
    return this.request<T>(path, { ...opts, method: "PUT", body });
  }

  async delete<T>(
    path: string,
    opts?: Omit<RequestOptions, "method" | "body">,
  ): Promise<Response<T>> {
    return this.request<T>(path, { ...opts, method: "DELETE" });
  }

  async patch<T>(
    path: string,
    body: unknown,
    opts?: Omit<RequestOptions, "method">,
  ): Promise<Response<T>> {
    return this.request<T>(path, { ...opts, method: "PATCH", body });
  }

  async request<T>(path: string, opts: RequestOptions): Promise<Response<T>> {
    const retryOpts: RetryOptions = { ...this.defaultRetry, ...opts.retry };
    let lastError: Error = new Error("no attempts made");

    for (let attempt = 0; attempt < retryOpts.maxAttempts; attempt++) {
      if (attempt > 0) {
        const delay = computeDelay(attempt - 1, retryOpts);
        this.emit("retry", { attempt, delay, path });
        await sleep(delay);
      }
      try {
        return await this._doRequest<T>(path, opts);
      } catch (err) {
        lastError = err as Error;
        if (err instanceof HttpError && !isRetryable(err.status)) {
          throw err;
        }
        this.emit("error", { attempt, error: err, path });
      }
    }
    throw new RetryExhaustedError(retryOpts.maxAttempts, lastError);
  }

  private async _doRequest<T>(
    path: string,
    opts: RequestOptions,
  ): Promise<Response<T>> {
    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    this.abortController = controller;

    const timeoutId = opts.timeout
      ? setTimeout(() => controller.abort(), opts.timeout)
      : null;

    const start = Date.now();
    try {
      const raw = await fetch(url, {
        method: opts.method,
        headers: { ...this.defaultHeaders, ...(opts.headers ?? {}) },
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
      });

      const latencyMs = Date.now() - start;
      const responseHeaders: Record<string, string> = {};
      raw.headers.forEach((value, key) => {
        responseHeaders[key] = value;
      });

      const text = await raw.text();
      if (!raw.ok) {
        throw new HttpError(raw.status, text);
      }

      const data: T = text ? (JSON.parse(text) as T) : ({} as T);
      this.emit("response", { status: raw.status, latencyMs, path });
      return { status: raw.status, headers: responseHeaders, data, latencyMs };
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
    }
  }

  abort(): void {
    this.abortController?.abort();
    this.abortController = null;
  }
}

export class PaginatedClient<T> {
  private readonly client: HttpClient;
  private readonly pageSize: number;

  constructor(client: HttpClient, pageSize = 20) {
    this.client = client;
    this.pageSize = pageSize;
  }

  async *fetchAll(
    path: string,
    params: Record<string, string> = {},
  ): AsyncGenerator<T[]> {
    let cursor: string | null = null;
    let page = 0;
    while (true) {
      const query = new URLSearchParams({
        ...params,
        limit: String(this.pageSize),
        ...(cursor ? { cursor } : { page: String(page) }),
      });
      const resp = await this.client.get<{ items: T[]; next_cursor?: string }>(
        `${path}?${query.toString()}`,
      );
      yield resp.data.items;
      if (!resp.data.next_cursor || resp.data.items.length < this.pageSize) {
        break;
      }
      cursor = resp.data.next_cursor;
      page++;
    }
  }

  async fetchPage(
    path: string,
    page: number,
    params: Record<string, string> = {},
  ): Promise<Response<T[]>> {
    const query = new URLSearchParams({
      ...params,
      limit: String(this.pageSize),
      page: String(page),
    });
    const resp = await this.client.get<{ items: T[] }>(
      `${path}?${query.toString()}`,
    );
    return { ...resp, data: resp.data.items };
  }
}

export function buildClient(
  baseUrl: string,
  apiKey: string,
  opts: { timeout?: number; retry?: Partial<RetryOptions> } = {},
): HttpClient {
  return new HttpClient(baseUrl, {
    headers: { Authorization: `Bearer ${apiKey}` },
    retry: opts.retry,
  });
}
