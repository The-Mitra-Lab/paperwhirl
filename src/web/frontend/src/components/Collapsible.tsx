import { useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";

interface CollapsibleProps {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
}

export default function Collapsible({
  title,
  children,
  defaultOpen = false,
}: CollapsibleProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="mb-4">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 rounded-lg bg-warm-100 text-left text-sm font-semibold text-warm-800 hover:text-teal-650 transition-colors cursor-pointer"
      >
        <ChevronRight
          size={16}
          className={`text-warm-400 transition-transform duration-200 ${
            open ? "rotate-90" : ""
          }`}
        />
        {title}
      </button>
      {open && (
        <div className="pt-3 px-3">
          {children}
          <div className="flex justify-center mt-3">
            <button
              onClick={() => setOpen(false)}
              className="w-3 h-3 bg-warm-800 rounded-sm hover:bg-teal-650 transition-colors cursor-pointer"
              aria-label="Close section"
            />
          </div>
        </div>
      )}
    </div>
  );
}
