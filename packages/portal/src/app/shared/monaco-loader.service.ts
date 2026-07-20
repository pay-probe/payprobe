import { Injectable } from "@angular/core";

/**
 * Loads the self-hosted Monaco editor (copied to `assets/monaco/vs` by the
 * Angular build) via its AMD loader, exactly once. Using the AMD loader keeps
 * Monaco out of the app bundle and avoids wiring its web workers through the
 * Angular builder — workers are served from a data: URL that importScripts the
 * self-hosted `workerMain.js`.
 */
@Injectable({ providedIn: "root" })
export class MonacoLoaderService {
  private loading: Promise<any> | null = null;

  load(): Promise<any> {
    const w = window as any;
    if (w.monaco) return Promise.resolve(w.monaco);
    if (this.loading) return this.loading;

    this.loading = new Promise((resolve, reject) => {
      const origin = document.baseURI.replace(/\/$/, "");
      const baseUrl = `${origin}/assets/monaco`;

      const boot = () => {
        const req = w.require;
        req.config({ paths: { vs: `${baseUrl}/vs` } });
        w.MonacoEnvironment = {
          getWorkerUrl: () => {
            const proxy = [
              `self.MonacoEnvironment = { baseUrl: '${baseUrl}/' };`,
              `importScripts('${baseUrl}/vs/base/worker/workerMain.js');`,
            ].join("\n");
            return `data:text/javascript;charset=utf-8,${encodeURIComponent(proxy)}`;
          },
        };
        req(
          ["vs/editor/editor.main"],
          () => resolve(w.monaco),
          (err: unknown) => reject(err),
        );
      };

      if (w.require) {
        boot();
        return;
      }
      const script = document.createElement("script");
      script.src = `${baseUrl}/vs/loader.js`;
      script.onload = boot;
      script.onerror = () =>
        reject(new Error("Failed to load the Monaco loader"));
      document.body.appendChild(script);
    });
    return this.loading;
  }
}
