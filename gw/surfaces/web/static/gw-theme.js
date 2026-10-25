/* Applied before first paint so the saved theme does not flash. */
(function(){try{var t=localStorage.getItem("gw-theme");if(t){document.documentElement.setAttribute("data-theme",t)}}catch{/* storage blocked (private mode): the default theme applies */}})();
