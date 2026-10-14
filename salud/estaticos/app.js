// Tooltips de las gráficas y registro del service worker.
"use strict";

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

const tip = document.querySelector(".tooltip");

function mostrar(valor, etiqueta, x, y) {
  // textContent: los textos vienen de datos, nunca como HTML.
  tip.replaceChildren();
  const fuerte = document.createElement("strong");
  fuerte.textContent = valor;
  tip.append(fuerte, document.createTextNode(etiqueta));
  tip.hidden = false;
  const ancho = tip.offsetWidth;
  tip.style.left = `${Math.min(Math.max(8, x - ancho / 2), window.innerWidth - ancho - 8)}px`;
  tip.style.top = `${Math.max(8, y - tip.offsetHeight - 12)}px`;
}

function ocultar() {
  tip.hidden = true;
}

// Líneas: un cursor vertical que salta al día más cercano.
for (const svg of document.querySelectorAll("svg.grafica[data-puntos]")) {
  const puntos = JSON.parse(svg.dataset.puntos);
  const cursor = svg.querySelector(".cursor");
  const punto = svg.querySelector(".cursor-punto");
  const arriba = Number(svg.dataset.arriba);
  const abajo = Number(svg.dataset.alto) - Number(svg.dataset.abajo);
  svg.setAttribute("tabindex", "0");
  let actual = puntos.length - 1;

  function enfocar(i) {
    actual = i;
    const [x, y, fecha, valor] = puntos[i];
    cursor.setAttribute("x1", x);
    cursor.setAttribute("x2", x);
    cursor.setAttribute("y1", arriba);
    cursor.setAttribute("y2", abajo);
    punto.setAttribute("cx", x);
    punto.setAttribute("cy", y);
    cursor.setAttribute("visibility", "visible");
    punto.setAttribute("visibility", "visible");
    const caja = svg.getBoundingClientRect();
    const escala = caja.width / svg.viewBox.baseVal.width;
    mostrar(valor, fecha, caja.left + x * escala, caja.top + y * escala);
  }

  function soltar() {
    cursor.setAttribute("visibility", "hidden");
    punto.setAttribute("visibility", "hidden");
    ocultar();
  }

  svg.addEventListener("pointermove", (e) => {
    const caja = svg.getBoundingClientRect();
    const x = ((e.clientX - caja.left) / caja.width) * svg.viewBox.baseVal.width;
    let mejor = 0;
    for (let i = 1; i < puntos.length; i++) {
      if (Math.abs(puntos[i][0] - x) < Math.abs(puntos[mejor][0] - x)) mejor = i;
    }
    enfocar(mejor);
  });
  svg.addEventListener("pointerleave", soltar);
  svg.addEventListener("focus", () => enfocar(actual));
  svg.addEventListener("blur", soltar);
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" && actual > 0) enfocar(actual - 1);
    else if (e.key === "ArrowRight" && actual < puntos.length - 1) enfocar(actual + 1);
    else return;
    e.preventDefault();
  });
}

// Columnas: cada una lleva su propio tooltip.
for (const marca of document.querySelectorAll("[data-tip]")) {
  const enseñar = () => {
    const caja = marca.getBoundingClientRect();
    const [etiqueta, valor] = marca.dataset.tip.split(": ");
    mostrar(valor, `Semana del ${etiqueta}`, caja.left + caja.width / 2, caja.top);
  };
  marca.addEventListener("pointerenter", enseñar);
  marca.addEventListener("focus", enseñar);
  marca.addEventListener("pointerleave", ocultar);
  marca.addEventListener("blur", ocultar);
}

// Botones de cantidad rápida
for (const boton of document.querySelectorAll("button[data-cantidad]")) {
  boton.addEventListener("click", () => {
    const input = document.getElementById("cantidad") || document.querySelector('input[name="cantidad"]');
    if (input) {
      input.value = boton.dataset.cantidad;
    }
  });
}

// Escáner de código de barras
const botonEscanear = document.querySelector("[data-escanear]");
if (
  botonEscanear &&
  "BarcodeDetector" in window &&
  navigator.mediaDevices &&
  typeof navigator.mediaDevices.getUserMedia === "function"
) {
  botonEscanear.hidden = false;

  botonEscanear.addEventListener("click", async () => {
    let detector;
    try {
      detector = new window.BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a"] });
    } catch (_err) {
      mostrarErrorEscaner("No se pudo iniciar el detector de códigos de barras.");
      return;
    }

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } },
      });
    } catch (_err) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: true });
      } catch (_err2) {
        mostrarErrorEscaner("No se pudo acceder a la cámara.");
        return;
      }
    }

    const contenedor = document.createElement("div");
    contenedor.className = "escaner-contenedor";

    const video = document.createElement("video");
    video.className = "escaner-video";
    video.setAttribute("playsinline", "true");
    video.srcObject = stream;

    const botonCerrar = document.createElement("button");
    botonCerrar.type = "button";
    botonCerrar.className = "boton secundario escaner-cerrar";
    botonCerrar.textContent = "Cerrar cámara";

    contenedor.append(video, botonCerrar);
    const form = botonEscanear.closest("form") || botonEscanear.parentElement;
    form.after(contenedor);

    try {
      await video.play();
    } catch (_err) {
      cerrar();
      mostrarErrorEscaner("No se pudo reproducir el vídeo de la cámara.");
      return;
    }

    let timer = null;

    function cerrar() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
      if (stream) {
        for (const pista of stream.getTracks()) {
          pista.stop();
        }
      }
      contenedor.remove();
    }

    botonCerrar.addEventListener("click", cerrar);

    timer = setInterval(async () => {
      if (video.readyState < 2) return;
      try {
        const codigos = await detector.detect(video);
        if (codigos && codigos.length > 0) {
          const valor = codigos[0].rawValue;
          cerrar();
          const campoCodigo = document.getElementById("codigo") || document.querySelector('input[name="codigo"]');
          if (campoCodigo) {
            campoCodigo.value = valor;
            const formActual = campoCodigo.closest("form");
            if (formActual) formActual.submit();
          }
        }
      } catch (_err) {
        // Ignorar fallos puntuales en un fotograma
      }
    }, 300);
  });
}

function mostrarErrorEscaner(mensaje) {
  const anterior = document.querySelector(".escaner-error");
  if (anterior) anterior.remove();
  const p = document.createElement("p");
  p.className = "aviso error escaner-error";
  p.textContent = mensaje;
  const form = document.querySelector(".formulario");
  if (form) {
    form.before(p);
  }
}
