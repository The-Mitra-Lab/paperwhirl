import { useState, useRef, useCallback, useEffect } from "react";
import { Upload, Loader2, X } from "lucide-react";

interface InputAreaProps {
  onSubmit: (file: File | null, identifier: string) => void;
  loading: boolean;
  /** Stage 5 E2: an identifier passed in from outside (e.g. a
   * Discuss-panel pill click) that should populate the input box
   * as if the user had pasted it. Each non-empty new value
   * overrides any existing input, mirroring the paste UX. */
  prefill?: string;
}

export default function InputArea({ onSubmit, loading, prefill }: InputAreaProps) {
  const [identifier, setIdentifier] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [fileName, setFileName] = useState<string | null>(null);
  const fileRef = useRef<File | null>(null);

  // Apply external prefill (pill clicks). Skip empty values so the
  // initial mount doesn't clear a user-typed identifier.
  useEffect(() => {
    if (prefill) setIdentifier(prefill);
  }, [prefill]);

  const handleFile = useCallback(
    (file: File) => {
      fileRef.current = file;
      setFileName(file.name);
      setIdentifier("");
      onSubmit(file, "");
    },
    [onSubmit]
  );

  const clearFile = useCallback(() => {
    fileRef.current = null;
    setFileName(null);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files[0];
      if (file?.type === "application/pdf") handleFile(file);
    },
    [handleFile]
  );

  const handleSubmit = () => {
    if (loading) return;
    if (fileRef.current) {
      onSubmit(fileRef.current, "");
    } else if (identifier.trim()) {
      onSubmit(null, identifier.trim());
    }
  };

  const canSubmit = !loading && (!!fileRef.current || !!identifier.trim());

  return (
    <div className="w-full max-w-xl mx-auto">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className={`relative rounded-xl border-2 transition-colors ${
          dragOver
            ? "border-teal-550 bg-teal-550/5 border-dashed"
            : fileName
              ? "border-teal-650/30 bg-teal-650/3"
              : "border-warm-200"
        }`}
      >
        {fileName ? (
          <div className="flex items-center gap-2 px-4 py-4">
            <Upload size={16} className="text-teal-650 shrink-0" />
            <span className="text-sm font-medium text-teal-650 truncate flex-1">
              {fileName}
            </span>
            <button
              onClick={clearFile}
              className="text-warm-400 hover:text-warm-700 cursor-pointer shrink-0"
            >
              <X size={14} />
            </button>
          </div>
        ) : (
          <input
            type="text"
            value={identifier}
            onChange={(e) => {
              setIdentifier(e.target.value);
              clearFile();
            }}
            onKeyDown={(e) =>
              e.key === "Enter" && canSubmit && handleSubmit()
            }
            placeholder="Drop a PDF or paste a DOI / PMID / PMC ID / URL"
            className="w-full px-4 py-4 text-sm text-warm-800 placeholder:text-warm-400 bg-transparent focus:outline-none rounded-xl"
          />
        )}
      </div>

      {loading && (
        <div className="mt-4 flex items-center justify-center gap-2 text-sm text-warm-400">
          <Loader2 size={16} className="animate-spin" />
          Generating...
        </div>
      )}
    </div>
  );
}
