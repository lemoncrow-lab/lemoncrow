/** Development-only bundle inspection; not included in the production build. */
import { build } from 'vite';
const chunks=[];
await build({
  mode:'production',
  plugins:[{
    name:'inspect-monaco-chunk',
    generateBundle(_opts,bundle){
      for (const [name,chunk] of Object.entries(bundle)) {
        if (chunk.type !== 'chunk' || !name.startsWith('assets/monaco-')) continue;
        const modules=Object.entries(chunk.modules).map(([path,info])=>({
          module:path.replace(/.*?\/node_modules\//,'node_modules/'), bytes:info.renderedLength||0
        })).sort((a,b)=>b.bytes-a.bytes);
        chunks.push({name,bytes:chunk.code.length,modules:modules.slice(0,45),totalModules:modules.length});
      }
    }
  }],
  logLevel:'error'
});
console.log(JSON.stringify(chunks,null,2));
