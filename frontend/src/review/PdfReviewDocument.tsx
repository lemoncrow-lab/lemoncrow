import { useEffect, useRef, useState } from "react";

interface PdfReviewDocumentProps {
  blob: Blob;
  label: string;
}

type PdfJs = typeof import("pdfjs-dist");
type PdfDocument = Awaited<ReturnType<PdfJs["getDocument"]>["promise"]>;
type RenderTask = ReturnType<Awaited<ReturnType<PdfDocument["getPage"]>>["render"]>;

let pdfRuntimePromise: Promise<PdfJs> | null = null;

function blobArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
  if (typeof blob.arrayBuffer === "function") return blob.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("PDF blob could not be read"));
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) resolve(reader.result);
      else reject(new Error("PDF blob did not produce binary data"));
    };
    reader.readAsArrayBuffer(blob);
  });
}

function loadPdfRuntime(): Promise<PdfJs> {
  if (!pdfRuntimePromise) {
    pdfRuntimePromise = Promise.all([
      import("pdfjs-dist"),
      import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
    ]).then(([pdfjs, worker]) => {
      pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
      return pdfjs;
    });
  }
  return pdfRuntimePromise;
}

export default function PdfReviewDocument({ blob, label }: PdfReviewDocumentProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [documentProxy, setDocumentProxy] = useState<PdfDocument | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [rendering, setRendering] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let disposed = false;
    let loadingTask: ReturnType<PdfJs["getDocument"]> | null = null;
    let loadedDocument: PdfDocument | null = null;

    setDocumentProxy(null);
    setPageNumber(1);
    setRendering(true);
    setError("");

    void (async () => {
      const [pdfjs, data] = await Promise.all([loadPdfRuntime(), blobArrayBuffer(blob)]);
      loadingTask = pdfjs.getDocument({ data });
      loadedDocument = await loadingTask.promise;
      if (disposed) {
        await loadingTask.destroy();
        return;
      }
      setDocumentProxy(loadedDocument);
    })().catch((reason: unknown) => {
      if (!disposed) {
        setRendering(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    });

    return () => {
      disposed = true;
      void loadingTask?.destroy();
    };
  }, [blob]);

  useEffect(() => {
    if (!documentProxy) return;
    let disposed = false;
    let renderTask: RenderTask | null = null;

    setRendering(true);
    setError("");
    void (async () => {
      const page = await documentProxy.getPage(pageNumber);
      if (disposed) return;
      const viewport = page.getViewport({ scale: 1.5 });
      const canvas = canvasRef.current;
      const context = canvas?.getContext("2d");
      if (!canvas || !context) throw new Error("PDF canvas is unavailable");

      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(viewport.width * pixelRatio));
      canvas.height = Math.max(1, Math.floor(viewport.height * pixelRatio));
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;

      renderTask = page.render({
        canvas,
        canvasContext: context,
        viewport,
        transform: pixelRatio === 1 ? undefined : [pixelRatio, 0, 0, pixelRatio, 0, 0],
      });
      await renderTask.promise;
      if (!disposed) setRendering(false);
    })().catch((reason: unknown) => {
      const name = reason instanceof Error ? reason.name : "";
      if (!disposed && name !== "RenderingCancelledException") {
        setRendering(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    });

    return () => {
      disposed = true;
      renderTask?.cancel();
    };
  }, [documentProxy, pageNumber]);

  const pageCount = documentProxy?.numPages ?? 0;

  return (
    <div className="w-full" data-testid="pdf-review-document">
      {pageCount > 0 && (
        <div className="mb-2 flex items-center justify-end border-b border-neutral-800 pb-2 font-mono text-[10px] text-neutral-500">
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-label={`Previous page of ${label}`}
              disabled={pageNumber <= 1}
              onClick={() => setPageNumber((current) => Math.max(1, current - 1))}
              className="review-icon-button h-6 w-7 disabled:opacity-30"
            >
              ←
            </button>
            <span className="min-w-16 text-center text-neutral-400">{pageNumber} / {pageCount}</span>
            <button
              type="button"
              aria-label={`Next page of ${label}`}
              disabled={pageNumber >= pageCount}
              onClick={() => setPageNumber((current) => Math.min(pageCount, current + 1))}
              className="review-icon-button h-6 w-7 disabled:opacity-30"
            >
              →
            </button>
          </div>
        </div>
      )}

      <div className="relative flex min-h-72 items-start justify-center overflow-auto bg-neutral-900 p-4">
        {error ? (
          <div className="my-auto px-6 py-12 text-center text-[11px] text-amber-300">PDF preview unavailable · {error}</div>
        ) : (
          <>
            <canvas
              ref={canvasRef}
              aria-label={`${label} PDF page ${pageNumber}`}
              className={`max-w-full bg-white shadow-[0_14px_45px_rgba(0,0,0,0.35)] ${rendering ? "opacity-40" : "opacity-100"}`}
            />
            {rendering && (
              <div className="absolute inset-0 flex items-center justify-center font-mono text-[10px] text-neutral-500">
                Rendering PDF…
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
