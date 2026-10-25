/* Gatewright sheets: CSRF on forms and HTMX, sortable tables, tooltips, theme, inline confirmations.
   One static file so the Content-Security-Policy can forbid inline script. */
(function(){
  var meta=document.querySelector('meta[name="csrf-token"]'),t=meta?meta.content:"";
  document.querySelectorAll('form[method="post"]').forEach(function(f){if(!f.querySelector('input[name="csrf"]')){var i=document.createElement("input");i.type="hidden";i.name="csrf";i.value=t;f.appendChild(i)}});
  document.body.addEventListener("htmx:configRequest",function(e){e.detail.headers["X-CSRF-Token"]=t});
})();
/* Sortable tables: click or Enter on a header; numbers, percentages and dates sort as values. */
(function(){
  function key(cell){var t=(cell.getAttribute("data-sort")||cell.textContent||"").trim();
    var n=parseFloat(t.replace(/[,%\s]/g,"").replace(/^[^0-9.+-]+/,""));
    return isNaN(n)?{n:null,t:t.toLowerCase()}:{n:n,t:t.toLowerCase()};}
  document.querySelectorAll(".tbl-wrap table").forEach(function(tbl){
    var head=tbl.rows[0];if(!head||!head.cells.length||head.cells[0].tagName!=="TH")return;
    Array.prototype.forEach.call(head.cells,function(th,i){
      if(!th.textContent.trim())return;
      th.tabIndex=0;th.style.cursor="pointer";th.setAttribute("aria-sort","none");
      function sort(){var asc=th.getAttribute("aria-sort")!=="ascending";
        Array.prototype.forEach.call(head.cells,function(h){if(h.hasAttribute("aria-sort"))h.setAttribute("aria-sort","none")});
        th.setAttribute("aria-sort",asc?"ascending":"descending");
        var rows=Array.prototype.slice.call(tbl.rows,1).filter(function(r){return r.cells.length===head.cells.length});
        rows.sort(function(a,b){var x=key(a.cells[i]),y=key(b.cells[i]);
          var c=(x.n!==null&&y.n!==null)?x.n-y.n:x.t.localeCompare(y.t);return asc?c:-c});
        var body=rows.length?rows[0].parentNode:null;rows.forEach(function(r){body.appendChild(r)});}
      th.addEventListener("click",sort);th.addEventListener("keydown",function(e){if(e.key==="Enter"){e.preventDefault();sort()}});
    });
  });
})();
(function(){
  var tip=document.getElementById("tip");
  function show(el){var t=el.getAttribute("data-tip");if(!t||!tip)return;tip.textContent=t;tip.style.display="block";
    var r=el.getBoundingClientRect();var x=Math.min(r.left,window.innerWidth-270);tip.style.left=Math.max(8,x)+"px";
    tip.style.top=(r.bottom+6)+"px";}
  function hide(){if(tip)tip.style.display="none"}
  document.addEventListener("mouseover",function(e){var el=e.target.closest("[data-tip]");if(el)show(el)});
  document.addEventListener("mouseout",function(e){if(e.target.closest("[data-tip]"))hide()});
  document.addEventListener("focusin",function(e){var el=e.target.closest("[data-tip]");if(el)show(el);else hide()});
  document.addEventListener("focusout",hide);
  /* theme toggle */
  document.addEventListener("click",function(e){if(!e.target.closest("[data-theme-toggle]"))return;
    var r=document.documentElement,cur=r.getAttribute("data-theme");
    var dark=cur?cur==="dark":window.matchMedia("(prefers-color-scheme: dark)").matches;
    var next=dark?"light":"dark";r.setAttribute("data-theme",next);try{localStorage.setItem("gw-theme",next)}catch{/* storage blocked (private mode): the choice lasts for this page */}});
  /* selects that submit their form on change (user switcher) */
  document.addEventListener("change",function(e){if(e.target.matches("select[data-autosubmit]"))e.target.form.submit()});
  /* confirmations are inline: a stamped button needs a second click */
  document.addEventListener("click",function(e){var b=e.target.closest(".confirm-btn");if(!b)return;
    if(b.getAttribute("data-armed")!=="1"){e.preventDefault();b.setAttribute("data-armed","1");
      b.setAttribute("data-label",b.textContent);b.textContent="Confirm "+b.textContent.toLowerCase();
      setTimeout(function(){if(b.getAttribute("data-armed")==="1"){b.removeAttribute("data-armed");b.textContent=b.getAttribute("data-label")}},5000);}});
})();
