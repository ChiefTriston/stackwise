document.addEventListener("DOMContentLoaded", () => {
  const clean = (value = "") =>
    value.replace(/\u00A0/g, " ").replace(/\s+/g, " ").trim();

  const isNoteText = (text) => {
    const t = clean(text).toLowerCase();
    return (
      t.startsWith("key differences:") ||
      t.startsWith("annual billing") ||
      t.startsWith("all plans include") ||
      t.startsWith("student discounts") ||
      t.startsWith("discounts available") ||
      t.startsWith("plans start at") ||
      t.startsWith("higher-tier plans") ||
      t.startsWith("pricing model changed") ||
      t.startsWith("monitor usage")
    );
  };

  const parsePriceIncludes = (raw) => {
    let text = clean(raw).replace(/^[—\-:]\s*/, "");
    if (!text) return { price: "", includes: "" };

    if (/^additional cost\s*:/i.test(text)) {
      text = text.replace(/^additional cost\s*:/i, "").trim();
    }

    const colonIndex = text.indexOf(":");
    const dashIndex = text.indexOf(" - ");
    const emDashIndex = text.indexOf(" — ");

    let splitIndex = -1;
    let splitLength = 0;

    if (colonIndex !== -1 && colonIndex < 120) {
      splitIndex = colonIndex;
      splitLength = 1;
    } else if (dashIndex !== -1) {
      splitIndex = dashIndex;
      splitLength = 3;
    } else if (emDashIndex !== -1) {
      splitIndex = emDashIndex;
      splitLength = 3;
    }

    if (splitIndex !== -1) {
      return {
        price: clean(text.slice(0, splitIndex)),
        includes: clean(text.slice(splitIndex + splitLength))
      };
    }

    return { price: text, includes: "" };
  };

  const extractStrongSegments = (p) => {
    const segments = [];
    let currentName = null;
    let currentText = "";

    [...p.childNodes].forEach((node) => {
      if (node.nodeType === Node.ELEMENT_NODE && node.tagName === "STRONG") {
        if (currentName) {
          segments.push({ name: clean(currentName), raw: clean(currentText) });
        }
        currentName = node.textContent;
        currentText = "";
      } else {
        currentText += " " + clean(node.textContent || "");
      }
    });

    if (currentName) {
      segments.push({ name: clean(currentName), raw: clean(currentText) });
    }

    return segments;
  };

  const parseParagraph = (p) => {
    const text = clean(p.textContent);
    if (!text) return { plans: [], notes: [] };

    if (isNoteText(text)) {
      return { plans: [], notes: [text] };
    }

    const strongSegments = extractStrongSegments(p);

    if (strongSegments.length > 1) {
      const plans = strongSegments.map((segment) => {
        const parsed = parsePriceIncludes(segment.raw);
        return {
          name: segment.name,
          price: parsed.price,
          includes: parsed.includes
        };
      }).filter(plan => clean(plan.name) && (clean(plan.price) || clean(plan.includes)));

      return { plans, notes: [] };
    }

    if (strongSegments.length === 1) {
      const parsed = parsePriceIncludes(strongSegments[0].raw);
      return {
        plans: [{
          name: strongSegments[0].name,
          price: parsed.price,
          includes: parsed.includes
        }],
        notes: []
      };
    }

    const colonIndex = text.indexOf(":");
    if (colonIndex !== -1 && colonIndex < 80) {
      const name = clean(text.slice(0, colonIndex));
      const parsed = parsePriceIncludes(text.slice(colonIndex + 1));
      return {
        plans: [{
          name,
          price: parsed.price,
          includes: parsed.includes
        }],
        notes: []
      };
    }

    return { plans: [], notes: [text] };
  };

  const pricingSections = [...document.querySelectorAll(".tool-section")].filter((section) => {
    const title = section.querySelector(".tool-section-title");
    return title && /^pricing\b/i.test(clean(title.textContent));
  });

  pricingSections.forEach((section) => {
    if (section.querySelector(".pricing-table-wrap")) return;

    const linkEl = [...section.querySelectorAll("a")].find(a =>
      /view full pricing details/i.test(clean(a.textContent))
    );

    const directPs = [...section.querySelectorAll(":scope > p")];
    const directLists = [...section.querySelectorAll(":scope > ul, :scope > ol")];

    let plans = [];
    let notes = [];
    let listNotes = [];

    directPs.forEach((p) => {
      if (linkEl && p.contains(linkEl)) return;

      const parsed = parseParagraph(p);
      plans = plans.concat(parsed.plans);
      notes = notes.concat(parsed.notes);
    });

    directLists.forEach((list) => {
      [...list.querySelectorAll("li")].forEach((li) => {
        const text = clean(li.textContent);
        if (text) listNotes.push(text);
      });
    });

    plans = plans
      .map((plan) => ({
        name: clean(plan.name),
        price: clean(plan.price),
        includes: clean(plan.includes)
      }))
      .filter((plan) => plan.name && (plan.price || plan.includes));

    const seen = new Set();
    plans = plans.filter((plan) => {
      const key = `${plan.name}|${plan.price}|${plan.includes}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });

    if (!plans.length) return;

    const wrap = document.createElement("div");
    wrap.className = "pricing-table-wrap";

    const box = document.createElement("div");
    box.className = "pricing-table-box";

    const table = document.createElement("table");
    table.className = "pricing-table";
    table.innerHTML = `
      <thead>
        <tr>
          <th>Plan</th>
          <th>Price</th>
          <th>Includes</th>
        </tr>
      </thead>
      <tbody></tbody>
    `;

    const tbody = table.querySelector("tbody");

    plans.forEach((plan) => {
      const tr = document.createElement("tr");

      const tdPlan = document.createElement("td");
      tdPlan.className = "pricing-col-plan";
      tdPlan.textContent = plan.name;

      const tdPrice = document.createElement("td");
      tdPrice.className = "pricing-col-price";
      tdPrice.textContent = plan.price || "—";
      if (/^free$/i.test(plan.price) || /^\$0\b/.test(plan.price)) {
        tdPrice.classList.add("free");
      }

      const tdIncludes = document.createElement("td");
      tdIncludes.className = "pricing-col-includes";
      tdIncludes.textContent = plan.includes || "—";

      tr.appendChild(tdPlan);
      tr.appendChild(tdPrice);
      tr.appendChild(tdIncludes);
      tbody.appendChild(tr);
    });

    box.appendChild(table);
    wrap.appendChild(box);

    if (notes.length || listNotes.length) {
      const notesWrap = document.createElement("div");
      notesWrap.className = "pricing-notes";

      notes.forEach((note) => {
        const line = document.createElement("div");
        line.className = "pricing-note-line";
        line.textContent = `△ ${note}`;
        notesWrap.appendChild(line);
      });

      if (listNotes.length) {
        const ul = document.createElement("ul");
        ul.className = "pricing-note-list";
        listNotes.forEach((item) => {
          const li = document.createElement("li");
          li.textContent = item;
          ul.appendChild(li);
        });
        notesWrap.appendChild(ul);
      }

      wrap.appendChild(notesWrap);
    }

    if (linkEl) {
      const linkClone = linkEl.cloneNode(true);
      linkClone.classList.add("pricing-source-link");
      wrap.appendChild(linkClone);
    }

    directPs.forEach((p) => p.classList.add("pricing-hide-original"));
    directLists.forEach((list) => list.classList.add("pricing-hide-original"));
    if (linkEl && linkEl.parentElement) {
      linkEl.parentElement.classList.add("pricing-hide-original");
    }

    section.appendChild(wrap);
  });
});