/** 在首页说明 Agent 当前可直接发起的六类决策，避免仅暴露内部 Skill 名称。 */
const capabilities = [
  ['工作 Offer 评估', '比较薪酬、成长、公司、地点、通勤与风险。'],
  ['旅行目的地比较', '比较天气、交通、预算、景点、餐饮和行程节奏。'],
  ['产品对比', '按预算、规格、实际使用需求、总成本与缺点比较。'],
  ['投资组合研究', '研究配置、集中度、风险、流动性与需要咨询的问题。'],
  ['课程与订阅评估', '比较学习目标、课程内容、时间投入、费用和使用概率。'],
  ['通用风险辩论', '梳理正反理由、硬约束、不可逆风险和未决问题。'],
]

export function SkillCapabilities() {
  return <section className="skill-capabilities" aria-label="可以帮助的决策类型">
    <h2>我可以帮助你做哪些决策？</h2>
    <div>{capabilities.map(([title, description]) => <article key={title}><strong>{title}</strong><span>{description}</span></article>)}</div>
  </section>
}
