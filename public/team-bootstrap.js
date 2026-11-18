import {initWorkActivity,localTeamApi} from './team-panel.js';
const activity=initWorkActivity({root:document.getElementById('team'),api:localTeamApi(),allowManualStart:true});
window.addEventListener('pagehide',()=>activity.destroy());
