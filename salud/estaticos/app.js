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

    try {
      await video.play();
    } catch (_err) {
      cerrar();
      mostrarErrorEscaner("No se pudo reproducir el vídeo de la cámara.");
      return;
    }

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

// Lectura de etiquetas con IA
for (const form of document.querySelectorAll("form[data-producto]")) {
  const botonLeer = form.querySelector("[data-leer-etiqueta]");
  const inputFoto = form.querySelector("[data-foto-etiqueta]");
  const avisosIa = form.querySelector("[data-avisos-ia]");

  if (botonLeer && inputFoto) {
    botonLeer.addEventListener("click", () => {
      inputFoto.click();
    });

    inputFoto.addEventListener("change", async () => {
      const fichero = inputFoto.files && inputFoto.files[0];
      if (!fichero) return;

      const textoOriginal = botonLeer.textContent;
      botonLeer.disabled = true;
      botonLeer.textContent = "Leyendo la etiqueta…";

      try {
        let bitmap;
        try {
          bitmap = await createImageBitmap(fichero, { imageOrientation: "from-image" });
        } catch (_err) {
          if (avisosIa) {
            avisosIa.replaceChildren();
            const p = document.createElement("p");
            p.className = "aviso error";
            p.textContent = "No se pudo abrir la foto";
            avisosIa.append(p);
          }
          return;
        }

        let ancho = bitmap.width;
        let alto = bitmap.height;
        const maxLado = 1600;
        if (ancho > maxLado || alto > maxLado) {
          if (ancho >= alto) {
            alto = Math.round((alto * maxLado) / ancho);
            ancho = maxLado;
          } else {
            ancho = Math.round((ancho * maxLado) / alto);
            alto = maxLado;
          }
        }

        const canvas = document.createElement("canvas");
        canvas.width = ancho;
        canvas.height = alto;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          if (avisosIa) {
            avisosIa.replaceChildren();
            const p = document.createElement("p");
            p.className = "aviso error";
            p.textContent = "No se pudo abrir la foto";
            avisosIa.append(p);
          }
          return;
        }
        ctx.drawImage(bitmap, 0, 0, ancho, alto);
        const blob = await new Promise((resolve) => {
          canvas.toBlob(resolve, "image/jpeg", 0.85);
        });
        if (!blob) {
          if (avisosIa) {
            avisosIa.replaceChildren();
            const p = document.createElement("p");
            p.className = "aviso error";
            p.textContent = "No se pudo abrir la foto";
            avisosIa.append(p);
          }
          return;
        }

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 120000);

        try {
          const resp = await fetch("/comida/etiqueta", {
            method: "POST",
            body: blob,
            headers: { "Content-Type": "image/jpeg" },
            signal: controller.signal,
          });
          clearTimeout(timeoutId);

          if (!resp.ok) {
            let errorMsg = "No se pudo leer la etiqueta. Rellénala a mano o prueba con otra foto.";
            try {
              const data = await resp.json();
              if (data && data.error) {
                errorMsg = data.error;
              }
            } catch (_err) {
              // Si no es JSON se mantiene el mensaje por defecto
            }
            if (avisosIa) {
              avisosIa.replaceChildren();
              const p = document.createElement("p");
              p.className = "aviso error";
              p.textContent = errorMsg;
              avisosIa.append(p);
            }
            return;
          }

          const etiqueta = await resp.json();

          const camposNutrientes = ["kcal", "proteinas", "hidratos", "grasas"];
          let algunNutrienteNull = false;

          for (const campo of camposNutrientes) {
            const input = form.querySelector(`[name="${campo}"]`);
            if (etiqueta[campo] !== null && etiqueta[campo] !== undefined) {
              if (input) {
                input.value = etiqueta[campo];
                marcarIA(input);
              }
            } else {
              algunNutrienteNull = true;
            }
          }

          if (etiqueta.unidad !== null && etiqueta.unidad !== undefined) {
            const inputUnidad = form.querySelector('[name="unidad"]');
            if (inputUnidad) {
              inputUnidad.value = etiqueta.unidad;
              marcarIA(inputUnidad);
            }
          }

          for (const campo of ["nombre", "marca"]) {
            const input = form.querySelector(`[name="${campo}"]`);
            if (input && !input.value.trim() && etiqueta[campo]) {
              input.value = etiqueta[campo];
              marcarIA(input);
            }
          }

          const inputIa = form.querySelector('input[name="ia"]');
          if (inputIa) {
            inputIa.value = "1";
          }

          if (avisosIa) {
            avisosIa.replaceChildren();
            const pPrinc = document.createElement("p");
            pPrinc.className = "aviso";
            pPrinc.textContent = "Valores leídos por IA a partir de la foto: revísalos antes de guardar.";
            avisosIa.append(pPrinc);

            if (Array.isArray(etiqueta.avisos)) {
              for (const aviso of etiqueta.avisos) {
                const pAv = document.createElement("p");
                pAv.className = "aviso";
                pAv.textContent = aviso;
                avisosIa.append(pAv);
              }
            }

            if (algunNutrienteNull) {
              const pFalta = document.createElement("p");
              pFalta.className = "aviso";
              pFalta.textContent = "Lo que falta no se ha podido leer: complétalo a mano.";
              avisosIa.append(pFalta);
            }
          }
        } catch (fetchErr) {
          clearTimeout(timeoutId);
          let errorMsg = "No se pudo leer la etiqueta. Rellénala a mano o prueba con otra foto.";
          if (fetchErr.name === "AbortError") {
            errorMsg = "La IA ha tardado demasiado. Rellénala a mano o prueba con otra foto.";
          }
          if (avisosIa) {
            avisosIa.replaceChildren();
            const p = document.createElement("p");
            p.className = "aviso error";
            p.textContent = errorMsg;
            avisosIa.append(p);
          }
        }
      } finally {
        botonLeer.disabled = false;
        botonLeer.textContent = textoOriginal;
        inputFoto.value = "";
      }
    });
  }

  function engancharLimpiarIA(input) {
    if (!input) return;
    const limpiar = () => {
      input.classList.remove("de-ia");
      if (input.id) {
        const label = form.querySelector(`label[for="${input.id}"]`);
        if (label) {
          const chip = label.querySelector(".chip-ia");
          if (chip) chip.remove();
        }
      }
      input.removeEventListener("input", limpiar);
      input.removeEventListener("change", limpiar);
    };

    input.addEventListener("input", limpiar, { once: true });
    input.addEventListener("change", limpiar, { once: true });
  }

  function marcarIA(input) {
    if (!input) return;
    input.classList.add("de-ia");
    if (input.id) {
      const label = form.querySelector(`label[for="${input.id}"]`);
      if (label && !label.querySelector(".chip-ia")) {
        const chip = document.createElement("span");
        chip.className = "chip-ia";
        chip.textContent = "IA";
        label.append(chip);
      }
    }
    engancharLimpiarIA(input);
  }

  for (const input of form.querySelectorAll(".de-ia")) {
    engancharLimpiarIA(input);
  }
}

const formConsejo = document.querySelector("form[data-consejo]");
if (formConsejo) {
  formConsejo.addEventListener("submit", () => {
    const boton = formConsejo.querySelector("button");
    if (boton) {
      boton.disabled = true;
      boton.textContent = "Enviando…";
    }
  });
}

const esperandoConsejo = document.querySelector("[data-esperando-consejo]");
if (esperandoConsejo) {
  const intervalo = setInterval(async () => {
    try {
      const resp = await fetch("/comida/consejo/estado");
      if (resp.ok) {
        const datos = await resp.json();
        if (!datos.preparando) {
          clearInterval(intervalo);
          location.replace("/comida#consejo");
        }
      }
    } catch {
      // Si el fetch falla, seguir intentando.
    }
  }, 4000);
}


