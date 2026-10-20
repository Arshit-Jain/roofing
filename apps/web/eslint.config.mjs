import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    files: ["**/*.{ts,tsx,js,jsx}"],
    rules: {
      "no-restricted-imports": ["error", {
        paths: [
          { name: ["firebase", "firestore"].join("/"), message: "The browser must never access Firestore." },
          { name: ["firebase", "storage"].join("/"), message: "Files are stored as backend-managed Firestore chunks." },
          { name: ["@firebase", "firestore"].join("/"), message: "The browser must never access Firestore." },
          { name: ["@firebase", "storage"].join("/"), message: "Files are stored as backend-managed Firestore chunks." }
        ]
      }]
    }
  },
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);
