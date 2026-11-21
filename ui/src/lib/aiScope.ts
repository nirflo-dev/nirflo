import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

export type Scope = "off" | "public" | "portfolio";
export const SCOPE_KEY = ["ai", "scope"] as const;

export const useAiScope = () =>
  useQuery({ queryKey: SCOPE_KEY, queryFn: ({ signal }) => api<{ scope: Scope }>("/ai/scope", undefined, { signal }), staleTime: 60_000, retry: 0 });

export function useSetAiScope() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (scope: Scope) =>
      api<{ scope: Scope }>("/ai/scope", undefined, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope }),
      }),
    onSuccess: (r) => qc.setQueryData(SCOPE_KEY, r),
  });
}