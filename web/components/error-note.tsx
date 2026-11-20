import type { ErrorView } from "@/lib/types";

export function ErrorNote({ error }: { error: ErrorView }) {
  return (
    <p className="alert" role="alert">
      {error.code}: {error.message}
    </p>
  );
}
