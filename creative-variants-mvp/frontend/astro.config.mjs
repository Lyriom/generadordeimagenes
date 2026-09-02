import { defineConfig } from "astro/config";

export default defineConfig({
  output: "static",
  server: {
    host: true
  },
  build: {
    assets: "_assets"
  },
  vite: {
    // El proxy de desarrollo va aquí y no en `server`: Astro solo acepta
    // host/port/headers/open en esa clave, así que antes se ignoraba y todas
    // las peticiones a /api daban 404 con `npm run dev`.
    //
    // El rewrite quita el prefijo /api porque el backend sirve en la raíz; es
    // el equivalente al `proxy_pass http://backend:8000/` de nginx en Docker.
    server: {
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, "")
        }
      }
    },
    build: {
      cssCodeSplit: false,
      rollupOptions: {
        output: {
          manualChunks: undefined
        }
      }
    }
  }
});
