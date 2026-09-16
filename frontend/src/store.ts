import { create } from "zustand";

/**
 * Client state, which is deliberately tiny: the cursor, the session being
 * viewed, and display preferences. Everything else is server state and lives in
 * TanStack Query, because two caches for one fact is how a card ends up
 * disagreeing with the timeline.
 */
interface UiState {
  sessionId: string | null;
  /** Replay cursor, epoch ms. Also the as-of: everything left of it is history. */
  cursor: number;
  nightMode: boolean;
  brightness: number;
  heatmapLayer: "efficiency" | "preference";
  setSession: (id: string | null) => void;
  setCursor: (t: number) => void;
  toggleNightMode: () => void;
  setBrightness: (v: number) => void;
  setHeatmapLayer: (l: "efficiency" | "preference") => void;
}

export const useUi = create<UiState>((set) => ({
  sessionId: null,
  cursor: 0,
  nightMode: true,
  brightness: 1,
  heatmapLayer: "efficiency",
  setSession: (sessionId) => set({ sessionId }),
  setCursor: (cursor) => set({ cursor }),
  toggleNightMode: () => set((s) => ({ nightMode: !s.nightMode })),
  setBrightness: (brightness) => set({ brightness }),
  setHeatmapLayer: (heatmapLayer) => set({ heatmapLayer }),
}));
